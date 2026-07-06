"""``dk transfer`` — copy Postgres app DB + ``django_media`` between managed environments."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from catalpa_tooling.compose import _compose
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.managed_deploy_env import ManagedDeployContext, load_managed_deploy_context
from catalpa_tooling.post_db_restore import run_post_db_restore_manage_commands
from catalpa_tooling.media_pull import run_pull_media, run_push_media
from catalpa_tooling.pgbackrest_db import (
    db_service_responds,
    pg_restore_compose_extras,
    run_drop_create_app_database,
    run_pg_dump_to_file,
    run_pg_restore,
)
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.remote_deploy import list_deploy_env_names
from catalpa_tooling.host_storage import ensure_host_storage
from catalpa_tooling.storage_config import volume_bind_kwargs
from catalpa_tooling.restic_files import (
    django_media_volume_name,
    resolve_env_with_compose_project,
)

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


def _collect_transfer_preflight_errors(
    *,
    src: str,
    dst: str,
    src_ctx: ManagedDeployContext,
    dst_ctx: ManagedDeployContext,
    src_r: dict[str, str],
    dst_r: dict[str, str],
    src_vol: str,
    dst_vol: str,
    do_db: bool,
    do_media: bool,
    config: ProjectConfig,
) -> list[str]:
    """Return human-readable errors; empty means preflight passed."""
    errs: list[str] = []
    sides: tuple[tuple[str, str, ManagedDeployContext, dict[str, str], str], ...] = (
        ("source", src, src_ctx, src_r, src_vol),
        ("destination", dst, dst_ctx, dst_r, dst_vol),
    )
    for label, env_name, ctx, env_r, vol_name in sides:
        svcs, hint = _compose_services_checked(ctx.compose_file, env_r)
        if svcs is None:
            errs.append(
                f"{label} (`{env_name}`): cannot list compose services for {ctx.compose_file!r}: {hint}. "
                "Check DOCKER_HOST and the compose file."
            )
            continue

        # Ensure external volumes exist before starting services that mount them.
        needs_volumes = (do_db and "db" in svcs) or do_media
        if needs_volumes and not _docker_volume_exists(vol_name, env_r):
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
                errs.append(
                    f"{label} (`{env_name}`): `ensure_external_stack_volumes` failed (exit {rc})."
                )
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
                _compose(ctx.compose_file, "up", "-d", "db", env_add=env_r, check=False)
                if not db_service_responds(ctx.compose_file, env_r):
                    errs.append(
                        f"{label} (`{env_name}`): `db` service did not become ready after "
                        f"`docker compose up -d db`."
                    )
        if do_media and not _docker_volume_exists(vol_name, env_r):
            errs.append(
                f"{label} (`{env_name}`): Docker volume {vol_name!r} not found even after "
                f"`ensure_external_stack_volumes`."
            )
    return errs


def _dest_writer_services(compose_file: str, env_add: dict[str, str]) -> list[str]:
    """Subset of ``_WRITER_SERVICE_CANDIDATES`` that exist in the destination compose project."""
    names = _compose_config_services(compose_file, env_add)
    return [s for s in _WRITER_SERVICE_CANDIDATES if s in names]


def _confirm_transfer_overwrite(
    dest_env: str,
    *,
    src_env: str,
    do_db: bool,
    do_media: bool,
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
        print("  - django_media volume (existing files removed, then tar extract)", file=sys.stderr)
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


def _start_dest_writers(compose_file: str, env_add: dict[str, str], *, dry_run: bool) -> None:
    """Bring the destination stack back up (``docker compose up -d``).

    Uses ``up -d`` instead of ``start <service>`` so that dependencies like ``redis``
    and ``migrate`` are also started, avoiding ordering / missing-dependency errors.
    """
    if dry_run:
        print(
            f"dry-run: would docker compose -f {compose_file} up -d",
            file=sys.stderr,
        )
        return
    r = _compose(compose_file, "up", "-d", env_add=env_add, check=False)
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
    src_vol = django_media_volume_name(
        str(src_r.get("COMPOSE_PROJECT_NAME") or ""), config=config
    )
    dst_vol = django_media_volume_name(
        str(dst_r.get("COMPOSE_PROJECT_NAME") or ""), config=config
    )

    print(
        f"  source: {src} compose={src_ctx.compose_file!r} DOCKER_HOST={src_ctx.docker_host!r} "
        f"django_media_volume={src_vol!r}",
        file=sys.stderr,
    )
    print(
        f"  dest:   {dst} compose={dst_ctx.compose_file!r} DOCKER_HOST={dst_ctx.docker_host!r} "
        f"django_media_volume={dst_vol!r}",
        file=sys.stderr,
    )
    print(f"  steps: db={do_db} media={do_media}", file=sys.stderr)

    preflight_errs = _collect_transfer_preflight_errors(
        src=src,
        dst=dst,
        src_ctx=src_ctx,
        dst_ctx=dst_ctx,
        src_r=src_r,
        dst_r=dst_r,
        src_vol=src_vol,
        dst_vol=dst_vol,
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
        dst, src_env=src, do_db=do_db, do_media=do_media
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

    if do_db or do_media:
        _stop_dest_writers(dst_ctx.compose_file, dst_ctx.env_add, dry_run=False)

    if do_db:
        print("transfer: pg_dump (source) …", file=sys.stderr)
        rc = run_pg_dump_to_file(src_ctx.compose_file, src_r, dump_path)
        if rc != 0:
            _start_dest_writers(dst_ctx.compose_file, dst_ctx.env_add, dry_run=False)
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
            _start_dest_writers(dst_ctx.compose_file, dst_ctx.env_add, dry_run=False)
            shutil.rmtree(session, ignore_errors=True)
            return rc
        print("transfer: pg_restore (destination) …", file=sys.stderr)
        rc = run_pg_restore(
            dst_ctx.compose_file,
            dst_r,
            pg_restore_compose_extras(
                ["--file", str(dump_path)],
                postgis=config.native.reset_db.postgis,
            ),
            config=config,
        )
        if rc != 0:
            try:
                dump_path.unlink(missing_ok=True)
            except OSError:
                pass
            _start_dest_writers(dst_ctx.compose_file, dst_ctx.env_add, dry_run=False)
            shutil.rmtree(session, ignore_errors=True)
            return rc
        try:
            dump_path.unlink(missing_ok=True)
        except OSError:
            pass

    if do_media:
        print("transfer: pull_media (source) …", file=sys.stderr)
        rc = run_pull_media(src_r, target=media_dir, dry_run=False, config=config)
        if rc != 0:
            _start_dest_writers(dst_ctx.compose_file, dst_ctx.env_add, dry_run=False)
            shutil.rmtree(session, ignore_errors=True)
            return rc
        print("transfer: push_media (destination) …", file=sys.stderr)
        rc = run_push_media(dst_r, source=media_dir, dry_run=False, config=config)
        if rc != 0:
            _start_dest_writers(dst_ctx.compose_file, dst_ctx.env_add, dry_run=False)
            shutil.rmtree(session, ignore_errors=True)
            return rc

    if do_db or do_media:
        _start_dest_writers(dst_ctx.compose_file, dst_ctx.env_add, dry_run=False)

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
        "--workdir",
        default=None,
        metavar="DIR",
        help=f"Parent directory for the transfer session (default: <repo>/{config.ops.transfer_workdir}).",
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
