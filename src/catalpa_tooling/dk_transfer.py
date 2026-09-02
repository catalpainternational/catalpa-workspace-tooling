"""``dk transfer`` — copy Postgres app DB + ``django_media`` between managed environments."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.compose import _compose
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dc_backup.hosts import (
    dc_backup_tls_extra_compose_files,
    merge_extra_compose_files,
)
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.managed_deploy_env import ManagedDeployContext, load_managed_deploy_context
from catalpa_tooling.post_db_restore import run_post_db_restore_manage_commands
from catalpa_tooling.media_rsync import TransferMediaMethod
from catalpa_tooling.media_storage import (
    MediaStorage,
    MediaStorageError,
    MediaStorageKind,
    resolve_media_storage,
)
from catalpa_tooling.media_transfer import run_transfer_media
from catalpa_tooling.pgbackrest_db import (
    compose_pg_restore_extras_for_config,
    db_service_responds,
    run_drop_create_app_database,
    run_pg_dump_to_file,
    run_pg_restore,
)
from catalpa_tooling.remote_deploy import list_deploy_env_names
from catalpa_tooling.host_storage import ensure_host_storage
from catalpa_tooling.local_proxy import (
    LocalProxyConfigError,
    local_proxy_extra_compose_files,
    sync_local_proxy_for_compose_action,
)
from catalpa_tooling.storage_config import volume_bind_kwargs
from catalpa_tooling.restic_files import resolve_env_with_compose_project

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
        from catalpa_tooling.pgbackrest_volume_config import ensure_external_stack_volumes

        rc = ensure_external_stack_volumes(env_r, config=config)
    if rc != 0:
        return f"{label} (`{env_name}`): `ensure_external_stack_volumes` failed (exit {rc})."
    return None


def _bind_dir_nonempty(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def _collect_transfer_preflight_errors(
    *,
    src: str,
    dst: str,
    src_ctx: ManagedDeployContext,
    dst_ctx: ManagedDeployContext,
    src_r: dict[str, str],
    dst_r: dict[str, str],
    src_media: MediaStorage | None,
    dst_media: MediaStorage | None,
    do_db: bool,
    do_media: bool,
    config: ProjectConfig,
) -> list[str]:
    """Return human-readable errors; empty means preflight passed."""
    errs: list[str] = []
    sides: tuple[
        tuple[str, str, ManagedDeployContext, dict[str, str], MediaStorage | None],
        ...,
    ] = (
        ("source", src, src_ctx, src_r, src_media),
        ("destination", dst, dst_ctx, dst_r, dst_media),
    )
    for label, env_name, ctx, env_r, media in sides:
        svcs, hint = _compose_services_checked(ctx.compose_file, env_r)
        if svcs is None:
            errs.append(
                f"{label} (`{env_name}`): cannot list compose services for {ctx.compose_file!r}: {hint}. "
                "Check DOCKER_HOST and the compose file."
            )
            continue

        media_needs_named_vol = (
            do_media
            and media is not None
            and media.kind is MediaStorageKind.VOLUME
        )
        # Ensure external volumes when DB needs them, or media uses a named volume.
        needs_volume_ensure = (do_db and "db" in svcs) or media_needs_named_vol
        vol_probe = media.location if media_needs_named_vol and media is not None else None
        if needs_volume_ensure and (
            vol_probe is None or not _docker_volume_exists(vol_probe, env_r)
        ):
            ensure_err = _ensure_stack_volumes(
                label=label, env_name=env_name, env_r=env_r, config=config
            )
            if ensure_err:
                errs.append(ensure_err)
                continue

        if do_db:
            if "db" not in svcs:
                errs.append(
                    f"{label} (`{env_name}`): compose file {ctx.compose_file!r} has no `db` service "
                    "(required for database transfer)."
                )
            elif not db_service_responds(ctx.compose_file, env_r):
                print(
                    f"transfer: {label} (`{env_name}`): `db` not responding, starting it …",
                    file=sys.stderr,
                )
                _compose(
                    ctx.compose_file,
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
                if not db_service_responds(ctx.compose_file, env_r):
                    errs.append(
                        f"{label} (`{env_name}`): `db` service did not become ready after "
                        f"`docker compose up -d db`."
                    )

        if do_media and media is not None:
            if media.kind is MediaStorageKind.VOLUME:
                if not _docker_volume_exists(media.location, env_r):
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


def _confirm_transfer_overwrite(
    dest_env: str,
    *,
    src_env: str,
    do_db: bool,
    do_media: bool,
    dst_media: MediaStorage | None = None,
    media_method: TransferMediaMethod = "rsync",
) -> bool:
    print(
        "WARNING: This will overwrite data on the destination environment:",
        file=sys.stderr,
    )
    if do_db:
        print(
            "  - PostgreSQL app database (drop + recreate empty DB, then pg_restore)",
            file=sys.stderr,
        )
    if do_media:
        where = dst_media.describe() if dst_media is not None else "django_media"
        if media_method == "tar":
            media_how = "existing files removed, then tar extract"
        else:
            media_how = "rsync --delete (tar fallback on failure)"
        print(
            f"  - media ({where}; {media_how})",
            file=sys.stderr,
        )
    print(f"  Source: {src_env}", file=sys.stderr)
    print(f"  Destination: {dest_env}", file=sys.stderr)
    print(file=sys.stderr)
    print(
        f"Type the destination environment name '{dest_env}' to confirm, or press Enter to cancel: ",
        file=sys.stderr,
        end="",
    )
    try:
        line = input()
    except EOFError:
        return False
    return line.strip() == dest_env


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
    repo_root = config.repo_root
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

    src_ctx = load_managed_deploy_context(config, src)
    if src_ctx is None:
        return 1
    dst_ctx = load_managed_deploy_context(config, dst)
    if dst_ctx is None:
        return 1

    src_r = resolve_env_with_compose_project(
        src_ctx.compose_file, src_ctx.env_add, config=config, dk_env_name=src
    )
    dst_r = resolve_env_with_compose_project(
        dst_ctx.compose_file, dst_ctx.env_add, config=config, dk_env_name=dst
    )

    src_media: MediaStorage | None = None
    dst_media: MediaStorage | None = None
    if do_media:
        try:
            src_media = resolve_media_storage(
                compose_file=src_ctx.compose_file, env=src_r, config=config
            )
            dst_media = resolve_media_storage(
                compose_file=dst_ctx.compose_file, env=dst_r, config=config
            )
        except MediaStorageError as exc:
            print(f"dk transfer: {exc}", file=sys.stderr)
            return 1

    print(
        f"  source: {src} compose={src_ctx.compose_file!r} DOCKER_HOST={src_ctx.docker_host!r}"
        + (f" media={src_media.describe()}" if src_media else ""),
        file=sys.stderr,
    )
    print(
        f"  dest:   {dst} compose={dst_ctx.compose_file!r} DOCKER_HOST={dst_ctx.docker_host!r}"
        + (f" media={dst_media.describe()}" if dst_media else ""),
        file=sys.stderr,
    )
    print(f"  steps: db={do_db} media={do_media}" + (f" media_method={media_method}" if do_media else ""), file=sys.stderr)

    preflight_errs = _collect_transfer_preflight_errors(
        src=src,
        dst=dst,
        src_ctx=src_ctx,
        dst_ctx=dst_ctx,
        src_r=src_r,
        dst_r=dst_r,
        src_media=src_media,
        dst_media=dst_media,
        do_db=do_db,
        do_media=do_media,
        config=config,
    )
    if preflight_errs:
        print("transfer: preflight failed:", file=sys.stderr)
        for line in preflight_errs:
            print(f"  - {line}", file=sys.stderr)
        return 1

    print("transfer: preflight ok.", file=sys.stderr)

    if dry:
        print("transfer: dry-run (no changes, no temp dir).", file=sys.stderr)
        return 0

    if not yes and not sys.stdin.isatty():
        print(
            "Refusing transfer without a TTY. Pass --yes if you intend to run non-interactively.",
            file=sys.stderr,
        )
        return 1
    if not yes and not _confirm_transfer_overwrite(
        dst,
        src_env=src,
        do_db=do_db,
        do_media=do_media,
        dst_media=dst_media,
        media_method=media_method,
    ):
        print("transfer: cancelled.", file=sys.stderr)
        return 1

    parent_raw = ns.workdir
    if parent_raw:
        parent = Path(parent_raw).expanduser()
        parent = parent.resolve() if parent.is_absolute() else (repo_root / parent).resolve()
    else:
        parent = repo_root / config.ops.transfer_workdir
    parent.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix=f"{src}_to_{dst}_", dir=str(parent)))
    print(f"transfer: session directory {session}", file=sys.stderr)

    dump_path = session / "pg.dump"
    media_dir = session / "media"
    dst_info = dst_ctx.info

    def restart_dest_writers() -> None:
        _start_dest_writers(
            dst_ctx.compose_file,
            dst_r,
            dry_run=False,
            config=config,
            env_name=dst,
            info=dst_info,
        )

    if do_db or do_media:
        _stop_dest_writers(dst_ctx.compose_file, dst_r, dry_run=False)

    if do_db:
        print("transfer: pg_dump (source) …", file=sys.stderr)
        rc = run_pg_dump_to_file(src_ctx.compose_file, src_r, dump_path)
        if rc != 0:
            restart_dest_writers()
            shutil.rmtree(session, ignore_errors=True)
            return rc
        print("transfer: drop + recreate app database (destination) …", file=sys.stderr)
        rc = run_drop_create_app_database(
            dst_ctx.compose_file,
            dst_r,
            postgis=config.native.reset_db.postgis,
        )
        if rc != 0:
            try:
                dump_path.unlink(missing_ok=True)
            except OSError:
                pass
            restart_dest_writers()
            shutil.rmtree(session, ignore_errors=True)
            return rc
        print("transfer: pg_restore (destination) …", file=sys.stderr)
        rc = run_pg_restore(
            dst_ctx.compose_file,
            dst_r,
            compose_pg_restore_extras_for_config(
                config,
                ["--file", str(dump_path)],
            ),
            config=config,
        )
        if rc != 0:
            try:
                dump_path.unlink(missing_ok=True)
            except OSError:
                pass
            restart_dest_writers()
            shutil.rmtree(session, ignore_errors=True)
            return rc
        try:
            dump_path.unlink(missing_ok=True)
        except OSError:
            pass

    if do_media:
        assert src_media is not None and dst_media is not None
        fallback_tar = media_method == "rsync"
        rc = run_transfer_media(
            src_r,
            dst_r,
            src_media=src_media,
            dst_media=dst_media,
            media_dir=media_dir,
            method=media_method,
            dry_run=False,
            config=config,
            fallback_tar=fallback_tar,
        )
        if rc != 0:
            restart_dest_writers()
            shutil.rmtree(session, ignore_errors=True)
            return rc

    if do_db or do_media:
        restart_dest_writers()

    if do_db:
        rc_hooks = run_post_db_restore_manage_commands(
            config,
            compose_file=dst_ctx.compose_file,
            env_add=dst_r,
            env_name=dst,
        )
        if rc_hooks != 0:
            if not ns.keep_workdir:
                shutil.rmtree(session, ignore_errors=True)
            return rc_hooks

    if not ns.keep_workdir:
        shutil.rmtree(session, ignore_errors=True)
    else:
        print(f"transfer: kept workdir {session}", file=sys.stderr)

    print("transfer: done.", file=sys.stderr)
    return 0


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
