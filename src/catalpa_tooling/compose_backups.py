"""pgBackRest and restic on a ``runtime: compose`` env: ``ComposeBackend``'s side of
``dk <env> db …``, ``files …`` and ``restore`` (``stack_commands`` dispatches).

The quadlet side is ``fcos.backend.Backend``: the render installs the config and units there,
so it has fewer subcommands, and they run over SSH against the units.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Literal

from catalpa_tooling.pgbackrest_volume_config import (
    PREFIX_READ,
    PREFIX_WRITE,
    _extract_stanza_vars,
    _validate_repo_vars,
    resolve_mode,
)

if TYPE_CHECKING:
    from catalpa_tooling.stack_backend import ComposeBackend


def pgbackrest_restore_configured(env: dict[str, str]) -> bool:
    """True when pgBackRest READ or WRITE credentials are complete in ``env``."""
    mode = resolve_mode(env)
    if mode not in ("read", "write"):
        return False
    prefix = PREFIX_WRITE if mode == "write" else PREFIX_READ
    vars_map = _extract_stanza_vars(env, prefix)
    return _validate_repo_vars(vars_map, mode=mode) is None


def _creds_path(b: ComposeBackend):
    return b.config.deploy_envs_dir / b.env_name / "credentials.yaml"


def ensure_backup_credentials(
    b: ComposeBackend, target: Literal["pgbackrest", "restic"], *, yes: bool, dry_run: bool, label: str
) -> int:
    """Create missing WRITE credentials in ``credentials.yaml`` (Spaces bucket + scoped key)."""
    from catalpa_tooling.doctl_spaces_provision import (
        ensure_spaces_backup_credentials,
        pgbr_write_configured,
        restic_write_configured,
    )

    configured = pgbr_write_configured if target == "pgbackrest" else restic_write_configured
    if configured(b.env):
        return 0
    return ensure_spaces_backup_credentials(
        b.config, b.env_name, b.env, _creds_path(b),
        target=target, command_label=label, dry_run=dry_run, yes=yes,
    )  # fmt: skip


def _install_systemd(b: ComposeBackend, ns: argparse.Namespace, *, dry_run: bool, only: str) -> int:
    from catalpa_tooling.systemd_remote_install import cmd_install_systemd_backups

    tail = [flag for flag, on in (("--dry-run", getattr(ns, "dry_run", False)), ("--enable", getattr(ns, "enable", False))) if on]
    return cmd_install_systemd_backups(
        b.config, b.env, str(b.ctx.docker_host), b.env_name, tail,
        global_dry_run=dry_run, yes=bool(getattr(ns, "yes", False)), fixed_only=only,
    )  # fmt: skip


def _db_running(b: ComposeBackend, why: str = "") -> bool:
    from catalpa_tooling.pgbackrest_db import db_service_responds

    if db_service_responds(b.ctx.compose_file, b.env):
        return True
    print(f"The `db` service is not running on the deployment host.{why}", file=sys.stderr)
    return False


def pgbackrest(b: ComposeBackend, sub: str, ns: argparse.Namespace, *, dry_run: bool) -> int:
    """Every ``dk <env> db`` subcommand but ``restore``."""
    from catalpa_tooling import pgbackrest_db as pg
    from catalpa_tooling.pgbackrest_volume_config import (
        materialize_configs,
        postgres_image_from_env,
        run_pgbackrest_verify,
    )

    config, env, compose_file = b.config, b.env, b.ctx.compose_file
    if sub in ("init", "configure"):
        img = postgres_image_from_env(env, config=config)
        print(f"Postgres image (volume ops / pgBackRest): {img}", file=sys.stderr)
    if sub == "init":
        rc = pg.run_bkp_db_init(compose_file, env, image=img, config=config, dk_env_name=b.env_name)
        if rc != 0 or not getattr(ns, "install_systemd", False):
            return rc
        return _install_systemd(b, ns, dry_run=dry_run, only="pgbackrest")
    if sub == "configure":
        rc = materialize_configs(env, dry_run=False, postgres_image=img, config=config)
        mode = getattr(ns, "configure_mode", None)
        if rc != 0 or mode is None:
            return rc
        if mode == "verify":
            rc = run_pgbackrest_verify(env, image=img)
            return rc if rc != 0 else pg.run_configure_verify_online_check(compose_file, env)
        if mode == "stanza-create":
            return pg.run_bkp_db_stanza_create_flow(
                compose_file, env, image=img, config=config, dk_env_name=b.env_name
            )
        print(f"Unknown db configure mode: {mode!r}", file=sys.stderr)
        return 1
    if sub == "install-systemd":
        return _install_systemd(b, ns, dry_run=dry_run, only="pgbackrest")
    if sub == "backup":
        if not _db_running(b, " Start the stack before online backup."):
            return 1
        return pg.run_backup(compose_file, env, ns.backup_type)
    if sub in ("info", "check", "version", "pgdump"):
        if not _db_running(b):
            return 1
        if sub == "pgdump":
            return pg.run_pg_dump(compose_file, env, list(getattr(ns, "pg_dump_args", None) or []))
        run = {"info": pg.run_info, "check": pg.run_check_online, "version": pg.run_version}[sub]
        return run(compose_file, env)
    print(f"Unknown db subcommand: {sub!r}", file=sys.stderr)
    return 1


def pgbackrest_restore(b: ComposeBackend, extra: list[str], *, yes: bool, dry_run: bool) -> int:
    """pgBackRest restore with the stack stopped (``pgbackrest_db.run_restore_offline``)."""
    from catalpa_tooling.pgbackrest_db import plan_restore_offline, run_restore_offline

    common = {"compose_file": b.ctx.compose_file, "env_name": b.env_name, "extra_pgbackrest_args": extra, "config": b.config}
    if dry_run:
        return plan_restore_offline(b.env, docker_host=str(b.env.get("DOCKER_HOST", "")), **common)
    if not yes and not sys.stdin.isatty():
        print("Refusing restore without a TTY. Pass --yes if you intend to run non-interactive.", file=sys.stderr)
        return 1
    return run_restore_offline(b.env, skip_confirm=yes, **common)


def restic(b: ComposeBackend, sub: str, ns: argparse.Namespace, *, dry_run: bool) -> int:
    """Every ``dk <env> files`` subcommand but ``restore``."""
    from catalpa_tooling import restic_files as r

    if sub == "push":
        return _push_media(b, ns, dry_run=dry_run)
    if sub == "install-systemd":
        return _install_systemd(b, ns, dry_run=dry_run, only="restic")
    env = dict(b.env)
    if sub == "backup":
        return r.run_backup(env, config=b.config)
    run = {"init": r.run_init, "snapshots": r.run_snapshots, "check": r.run_check, "stats": r.run_stats}.get(sub)
    if run is None:
        print(f"Unknown files subcommand: {sub!r}", file=sys.stderr)
        return 1
    return run(env)


def _push_media(b: ComposeBackend, ns: argparse.Namespace, *, dry_run: bool) -> int:
    """``files push``: mirror local media into the env's volume (rsync ``--delete``)."""
    from catalpa_tooling.cli_confirm import confirm_by_typing_env_name
    from catalpa_tooling.env_handlers import _ensure_stack_volumes
    from catalpa_tooling.media_rsync import (
        resolve_push_media_source,
        run_push_media_rsync,
    )

    source = resolve_push_media_source(b.config, b.config.repo_root, ns.source)
    if source is None:
        return 1
    rc = _ensure_stack_volumes(
        b.config, b.env_name, b.ctx.info, b.env, b.ctx.storage_volumes, dry_run=dry_run
    )
    if rc != 0:
        return rc
    if not ns.yes and not dry_run:
        if not sys.stdin.isatty():
            print("Refusing files push without a TTY. Pass --yes for non-interactive use.", file=sys.stderr)
            return 1
        print(
            "WARNING: This mirrors host media into the deployment volume "
            "(rsync --delete removes files on the volume that are not on the host).",
            file=sys.stderr,
        )
        if not confirm_by_typing_env_name(b.env_name):
            print("files push cancelled.", file=sys.stderr)
            return 1
    return run_push_media_rsync(
        b.env, source=source, dry_run=dry_run, method=ns.method, alpine_image=str(ns.image), config=b.config
    )


def files_restore(b: ComposeBackend, snapshot: str, *, yes: bool, dry_run: bool) -> int:
    """Replace the media volumes with a restic snapshot (``restic_files.run_restore``)."""
    from catalpa_tooling.restic_files import run_restore

    if dry_run:
        volumes = list(b.config.ops.restic.data_volumes) if b.config.has_ops else []
        what = f"volumes {', '.join(volumes)}" if volumes else "media"
        print(
            f"dry-run: would replace {b.env_name}'s {what} with restic snapshot {snapshot!r} "
            "(files not in the snapshot are deleted)",
            file=sys.stderr,
        )
        return 0
    if not yes and not sys.stdin.isatty():
        print("Refusing restore without a TTY. Pass --yes if you intend to run non-interactive.", file=sys.stderr)
        return 1
    return run_restore(dict(b.env), snapshot, env_name=b.env_name, skip_confirm=yes, config=b.config)


def restore_all(b: ComposeBackend, extra: list[str], snapshot: str, *, yes: bool, dry_run: bool) -> int:
    """``dk <env> restore``: ``db restore`` then ``files restore``, for the kinds this env backs
    up or restores, after one confirmation."""
    from catalpa_tooling.backup_settings import BackupSettingsError, backup_settings
    from catalpa_tooling.stack_commands import db_restore

    env_name = b.env_name
    try:
        roles = backup_settings(b.config, env_name, b.ctx.info)
    except BackupSettingsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    kinds = [k for k in ("db", "files") if roles.writes(k) or roles.reads(k)]
    if not kinds:
        print(
            f"{env_name} has nothing to restore from: set backups: or restore_from: in its info.yaml.",
            file=sys.stderr,
        )
        return 1
    if not dry_run and not yes:
        from catalpa_tooling.cli_confirm import confirm_by_typing_env_name

        src = f"{roles.restore_from}'s" if roles.restore_from else f"{env_name}'s own"
        print(
            f"WARNING: replace {env_name}'s {' and '.join({'db': 'database', 'files': 'media'}[k] for k in kinds)} "
            f"with {src} backups; what they don't have is lost.",
            file=sys.stderr,
        )
        if not sys.stdin.isatty() or not confirm_by_typing_env_name(env_name):
            print("restore: cancelled.", file=sys.stderr)
            return 1
    if "db" in kinds:
        rc = db_restore(b, pgbackrest_args=extra, yes=True, dry_run=dry_run)
        if rc != 0:
            return rc
    if "files" in kinds:
        return files_restore(b, snapshot, yes=True, dry_run=dry_run)
    return 0
