"""``dk transfer`` — copy Postgres app DB + ``django_media`` between managed environments.

The command (argument checks) and the compose pieces the transfer uses: preflight, stopping and
starting the destination's writers. The transfer itself is ``stack_transfer.run_transfer``, the
same for compose and quadlet envs.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.compose import _compose
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dc_backup.hosts import (
    dc_backup_tls_extra_compose_files,
    merge_extra_compose_files,
)
from catalpa_tooling.host_storage import ensure_host_storage
from catalpa_tooling.local_proxy import (
    LocalProxyConfigError,
    local_proxy_extra_compose_files,
    sync_local_proxy_for_compose_action,
)
from catalpa_tooling.media_rsync import TransferMediaMethod
from catalpa_tooling.media_storage import MediaStorage, MediaStorageKind
from catalpa_tooling.pgbackrest_db import db_service_responds
from catalpa_tooling.remote_deploy import list_deploy_env_names
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.storage_config import volume_bind_kwargs

# Optional writers to quiet during transfer (only if present in the destination compose file).
_WRITER_SERVICE_CANDIDATES: tuple[str, ...] = ("django", "caddy")


def _merge_process_env(env: dict[str, str]) -> dict[str, str]:
    """Process env for ``docker compose`` / ``docker volume`` (honors ``DOCKER_HOST``, etc.)."""
    out = os.environ.copy()
    for k, v in env.items():
        if v is not None:
            out[k] = str(v)
    return out


def _compose_config_services(compose_file: str, env_add: dict[str, str]) -> frozenset[str]:
    """Service names from ``docker compose config --services`` (empty on failure)."""
    r = run_cmd(
        ["docker", "compose", "-f", compose_file, "config", "--services"],
        env=_merge_process_env(env_add),
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode != 0:
        return frozenset()
    return frozenset(ln.strip() for ln in r.stdout.splitlines() if ln.strip())


def _compose_services_checked(
    compose_file: str, env: dict[str, str]
) -> tuple[frozenset[str] | None, str | None]:
    """List compose services, or ``(None, reason)`` on failure / empty output."""
    r = run_cmd(
        ["docker", "compose", "-f", compose_file, "config", "--services"],
        env=_merge_process_env(env),
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode != 0:
        hint = (r.stderr or r.stdout or "").strip()
        return None, (hint[:500] if hint else "docker compose config --services failed")
    names = frozenset(ln.strip() for ln in r.stdout.splitlines() if ln.strip())
    if not names:
        return None, "compose listed no services (unexpected empty output)"
    return names, None


def _docker_volume_exists(name: str, env: dict[str, str]) -> bool:
    r = run_cmd(
        ["docker", "volume", "inspect", name],
        env=_merge_process_env(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    return r.returncode == 0


def _ensure_stack_volumes(
    *,
    label: str,
    env_name: str,
    env_r: dict[str, str],
    config: ProjectConfig,
) -> str | None:
    """Create missing external stack volumes. Return an error string, or None on success."""
    print(
        f"transfer: {label} (`{env_name}`): ensuring external volumes …",
        file=sys.stderr,
    )
    bind_kwargs = volume_bind_kwargs(config, env_name)
    if bind_kwargs:
        import yaml

        info_path = config.deploy_envs_dir / env_name / "info.yaml"
        with open(info_path, encoding="utf-8") as f:
            info = yaml.safe_load(f) or {}
        from catalpa_tooling.storage_config import parse_storage_volumes_from_info

        specs = parse_storage_volumes_from_info(info, config)
        rc = ensure_host_storage(
            config,
            env_name,
            info,
            specs,
            env_add=env_r,
        )
    else:
        from catalpa_tooling.pgbackrest_volume_config import (
            ensure_external_stack_volumes,
        )

        rc = ensure_external_stack_volumes(env_r, config=config)
    if rc != 0:
        return f"{label} (`{env_name}`): `ensure_external_stack_volumes` failed (exit {rc})."
    return None


def _missing_stack_volumes(env_r: dict[str, str], config: ProjectConfig) -> list[str]:
    """The stack's external volumes ``_ensure_stack_volumes`` would create (a dry run)."""
    from catalpa_tooling.pgbackrest_volume_config import _stack_volumes_with_compose_keys

    names = [name for name, _key in _stack_volumes_with_compose_keys(env_r, config=config)]
    return [n for n in names if not _docker_volume_exists(n, env_r)]


def _bind_dir_nonempty(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def compose_preflight_errors(
    *,
    label: str,
    env_name: str,
    compose_file: str,
    env_r: dict[str, str],
    media: MediaStorage | None,
    do_db: bool,
    do_media: bool,
    config: ProjectConfig,
    dry_run: bool = False,
) -> list[str]:
    """One compose end's transfer preflight (``label``: source or destination): its services
    list, its external volumes (created when missing), db started and answering, the media
    present (a source bind directory must not be empty). Human-readable errors; empty means ok.

    ``dry_run`` (``--dry-run``) changes nothing: a stopped db and missing volumes are
    printed as what a real run would do, not errors, so the rest of the check still reports.
    """
    errs: list[str] = []
    where = f"{label} (`{env_name}`)"
    would_create: list[str] = []
    svcs, hint = _compose_services_checked(compose_file, env_r)
    if svcs is None:
        errs.append(
            f"{label} (`{env_name}`): cannot list compose services for {compose_file!r}: {hint}. "
            "Check DOCKER_HOST and the compose file."
        )
        return errs

    media_needs_named_vol = (
        do_media
        and media is not None
        and media.kind is MediaStorageKind.VOLUME
    )
    # Ensure external volumes when DB needs them, or media uses a named volume.
    needs_volume_ensure = (do_db and "db" in svcs) or media_needs_named_vol
    vol_probe = media.location if media_needs_named_vol and media is not None else None
    if needs_volume_ensure and dry_run:
        would_create = _missing_stack_volumes(env_r, config)
        if would_create:
            print(
                f"transfer: {where}: external volumes missing; a real transfer would create "
                f"them: {', '.join(would_create)}",
                file=sys.stderr,
            )
    elif needs_volume_ensure and (
        vol_probe is None or not _docker_volume_exists(vol_probe, env_r)
    ):
        ensure_err = _ensure_stack_volumes(
            label=label, env_name=env_name, env_r=env_r, config=config
        )
        if ensure_err:
            errs.append(ensure_err)
            return errs

    if do_db:
        if "db" not in svcs:
            errs.append(
                f"{label} (`{env_name}`): compose file {compose_file!r} has no `db` service "
                "(required for database transfer)."
            )
        elif dry_run:
            if not db_service_responds(compose_file, env_r):
                print(f"transfer: {where}: db not running; a real transfer would start it.", file=sys.stderr)
        elif not db_service_responds(compose_file, env_r):
            print(
                f"transfer: {label} (`{env_name}`): `db` not responding, starting it …",
                file=sys.stderr,
            )
            _compose(
                compose_file,
                "up",
                "-d",
                "db",
                env_add=env_r,
                extra_compose_files=merge_extra_compose_files(
                    dc_backup_tls_extra_compose_files(
                        {},
                        config,
                        env_name,
                        env_r,
                        ["up"],
                    )
                )
                if config is not None
                else None,
                check=False,
            )
            if not db_service_responds(compose_file, env_r):
                errs.append(
                    f"{label} (`{env_name}`): `db` service did not become ready after "
                    f"`docker compose up -d db`."
                )

    if do_media and media is not None:
        if media.kind is MediaStorageKind.VOLUME:
            if media.location in would_create:
                pass  # reported above: a real run creates it
            elif not _docker_volume_exists(media.location, env_r):
                errs.append(
                    f"{label} (`{env_name}`): Docker volume {media.location!r} not found even after "
                    f"`ensure_external_stack_volumes`."
                )
        else:
            host = Path(media.location)
            if not host.is_dir():
                errs.append(
                    f"{label} (`{env_name}`): media bind path is not a directory: {host}"
                )
            elif label == "source" and not _bind_dir_nonempty(host):
                errs.append(
                    f"{label} (`{env_name}`): media bind path is empty: {host} "
                    "(refusing to overwrite destination with no files)."
                )
    return errs


def _dest_writer_services(compose_file: str, env_add: dict[str, str]) -> list[str]:
    """Subset of ``_WRITER_SERVICE_CANDIDATES`` that exist in the destination compose project."""
    names = _compose_config_services(compose_file, env_add)
    return [s for s in _WRITER_SERVICE_CANDIDATES if s in names]


def _read_deploy_info(config: ProjectConfig, env_name: str) -> dict[str, Any]:
    """Load ``docker/envs/<env>/info.yaml`` (empty mapping if missing/invalid)."""
    path = config.deploy_envs_dir / env_name / "info.yaml"
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def _transfer_extra_compose_files(
    *,
    config: ProjectConfig,
    env_name: str,
    info: dict[str, Any],
    env_add: dict[str, str],
    compose_args: list[str],
) -> list[str] | None:
    """Local-proxy + DC-backup TLS overrides for transfer ``compose up`` (same as ``dk <env>``)."""
    proxy_files = local_proxy_extra_compose_files(
        info,
        config,
        env_name,
        env_add,
        compose_args,
    )
    tls_files = dc_backup_tls_extra_compose_files(
        info,
        config,
        env_name,
        env_add,
        compose_args,
    )
    return merge_extra_compose_files(proxy_files, tls_files)


def _stop_dest_writers(compose_file: str, env_add: dict[str, str], *, dry_run: bool) -> None:
    """Stop optional writer services on the destination (each service separately; missing names skipped)."""
    services = _dest_writer_services(compose_file, env_add)
    if dry_run:
        svcs = " ".join(services) if services else "(none — not in compose file)"
        print(
            f"dry-run: would docker compose -f {compose_file} stop {svcs}",
            file=sys.stderr,
        )
        return
    for svc in services:
        r = _compose(compose_file, "stop", svc, env_add=env_add, check=False)
        if r.returncode != 0:
            print(
                f"transfer: warning: `compose stop {svc}` returned {r.returncode} "
                "(service may be absent or already stopped); continuing.",
                file=sys.stderr,
            )


def _start_dest_writers(
    compose_file: str,
    env_add: dict[str, str],
    *,
    dry_run: bool,
    config: ProjectConfig,
    env_name: str,
    info: dict[str, Any] | None = None,
) -> None:
    """Bring the destination stack back up (``docker compose up -d``).

    Uses ``up -d`` instead of ``start <service>`` so that dependencies like ``redis``
    and ``migrate`` are also started, avoiding ordering / missing-dependency errors.

    Applies the same local-proxy compose override as ``dk <env> up`` so stack Caddy does
    not publish host :80/:443 while ``catalpa-local-proxy`` holds those ports.
    """
    compose_args = ["up", "-d"]
    info_map = info if info is not None else _read_deploy_info(config, env_name)
    if dry_run:
        print(
            f"dry-run: would docker compose -f {compose_file} up -d"
            " (with local-proxy / dc-backup-tls overrides when applicable)",
            file=sys.stderr,
        )
        return
    try:
        proxy_rc = sync_local_proxy_for_compose_action(
            info_map,
            config,
            env_name,
            compose_args,
            env_add,
            dry_run=False,
        )
    except LocalProxyConfigError as exc:
        print(f"transfer: warning: local proxy sync failed: {exc}", file=sys.stderr)
        proxy_rc = 1
    if proxy_rc != 0:
        print(
            "transfer: warning: local proxy sync returned "
            f"{proxy_rc}; continuing with compose up.",
            file=sys.stderr,
        )
    try:
        extra_compose_files = _transfer_extra_compose_files(
            config=config,
            env_name=env_name,
            info=info_map,
            env_add=env_add,
            compose_args=compose_args,
        )
    except LocalProxyConfigError as exc:
        print(f"transfer: warning: local proxy override failed: {exc}", file=sys.stderr)
        extra_compose_files = merge_extra_compose_files(
            dc_backup_tls_extra_compose_files(
                info_map,
                config,
                env_name,
                env_add,
                compose_args,
            )
        )
    r = _compose(
        compose_file,
        *compose_args,
        env_add=env_add,
        extra_compose_files=extra_compose_files,
        check=False,
    )
    if r.returncode != 0:
        print(
            f"transfer: warning: `compose up -d` returned {r.returncode}; "
            "check the stack manually.",
            file=sys.stderr,
        )


def cmd_transfer(ns: argparse.Namespace, config: ProjectConfig) -> int:
    """Run ``dk transfer`` (see docs/DK.md)."""
    src = ns.source_env
    dst = ns.dest_env
    dry = bool(ns.dry_run)
    yes = bool(ns.yes)
    media_method: TransferMediaMethod = getattr(ns, "media_method", None) or "rsync"
    if media_method not in ("rsync", "tar"):
        print(f"dk transfer: invalid --media-method {media_method!r}.", file=sys.stderr)
        return 1

    if src == dst:
        print("dk transfer: source and destination must differ.", file=sys.stderr)
        return 1

    names = set(list_deploy_env_names(config.deploy_envs_dir))
    if src not in names:
        print(f"dk transfer: unknown source environment {src!r}.", file=sys.stderr)
        return 1
    if dst not in names:
        print(f"dk transfer: unknown destination environment {dst!r}.", file=sys.stderr)
        return 1

    if ns.db and ns.media:
        do_db = do_media = True
    elif ns.db:
        do_db, do_media = True, False
    elif ns.media:
        do_db, do_media = False, True
    else:
        do_db = do_media = True

    from catalpa_tooling.stack_backend import BackendError, backend_for
    from catalpa_tooling.stack_transfer import run_transfer

    try:
        src_b = backend_for(config, src)
        dst_b = backend_for(config, dst)
    except BackendError as exc:
        print(f"dk transfer: {exc}", file=sys.stderr)
        return 1
    return run_transfer(
        config, src_b=src_b, dst_b=dst_b, do_db=do_db, do_media=do_media, media_method=media_method,
        dry=dry, yes=yes, workdir=ns.workdir, keep_workdir=bool(ns.keep_workdir),
    )  # fmt: skip


def populate_transfer_arguments(parser: argparse.ArgumentParser, config: ProjectConfig) -> None:
    """Add ``dk transfer`` flags and positionals to ``parser``."""
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run preflight checks only (no transfer); exit 1 if preflight fails.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip typing the destination env name to confirm (required without a TTY).",
    )
    parser.add_argument(
        "--db",
        action="store_true",
        help="Select the database leg (see epilog).",
    )
    parser.add_argument(
        "--media",
        action="store_true",
        help="Select the django_media leg (see epilog).",
    )
    parser.add_argument(
        "--media-method",
        choices=("rsync", "tar"),
        default="rsync",
        help=(
            "How to copy django_media (default: rsync --delete; "
            "tar = wipe destination then full archive extract)."
        ),
    )
    parser.add_argument(
        "--workdir",
        default=None,
        metavar="DIR",
        help=(
            "Parent directory for the transfer session "
            f"(default: <repo>/{config.ops.transfer_workdir if config.has_ops else '<ops.transfer_workdir>'})."
        ),
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Keep the temporary session directory after success.",
    )
    parser.add_argument("source_env", help="docker/envs/<name>/ with info.yaml (copy from).")
    parser.add_argument("dest_env", help="docker/envs/<name>/ with info.yaml (copy into).")


def build_transfer_arg_parser(config: ProjectConfig) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dk transfer",
        description=(
            "Copy PostgreSQL app data and django_media from one docker/envs/ environment to another. "
            "Media defaults to incremental rsync (--delete); use --media-method tar for a full archive. "
            "Uses a temporary directory under --workdir (see docs/DK.md)."
        ),
        epilog=(
            "What to copy: pass neither flag for DB + media; only --db for database only; "
            "only --media for django_media only; both flags for DB + media."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    populate_transfer_arguments(p, config)
    return p
