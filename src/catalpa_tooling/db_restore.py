"""Unified ``dk <env> db restore`` orchestration (pgBackRest vs local dumps)."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.fetch_db import (
    configured_app_dump_exists,
    configured_metabase_dump_exists,
    run_fetch_all_dbs,
)
from catalpa_tooling.pgbackrest_db import (
    compose_pg_restore_extras_for_config,
    ensure_db_service_running,
    plan_restore_offline,
    run_drop_create_app_database,
    run_drop_create_metabase_database,
    run_pg_restore,
    run_restore_offline,
)
from catalpa_tooling.pgbackrest_volume_config import (
    PREFIX_READ,
    PREFIX_WRITE,
    _extract_stanza_vars,
    _validate_repo_vars,
    resolve_mode,
)
from catalpa_tooling.post_db_restore import (
    run_post_db_restore_manage_commands,
    run_post_metabase_db_restore_manage_commands,
)


def pgbackrest_restore_configured(env: dict[str, str]) -> bool:
    """True when pgBackRest READ or WRITE credentials are complete in ``env``."""
    mode = resolve_mode(env)
    if mode not in ("read", "write"):
        return False
    prefix = PREFIX_WRITE if mode == "write" else PREFIX_READ
    vars_map = _extract_stanza_vars(env, prefix)
    return _validate_repo_vars(vars_map, mode=mode) is None


def run_compose_app_dump_restore(
    config: ProjectConfig,
    *,
    compose_file: str,
    env_add: dict[str, str],
    env_name: str,
    archive_path: Path | None = None,
    extra_pg_restore_args: Sequence[str] | None = None,
) -> int:
    """Drop/create app DB, pg_restore from ``archive_path`` or default, then post hooks."""
    extras = list(extra_pg_restore_args or [])
    if archive_path is not None:
        extras = ["--file", str(archive_path), *extras]
    restore_extras = compose_pg_restore_extras_for_config(
        config,
        extras,
        default_archive=config.fetch_db_dump_path,
    )
    if "--file" not in restore_extras and sys.stdin.isatty():
        return 1
    rc = ensure_db_service_running(
        compose_file, env_add, config=config, dk_env_name=env_name
    )
    if rc != 0:
        return rc
    print(
        "db restore: replacing app database with an empty database before restore …",
        file=sys.stderr,
    )
    rc = run_drop_create_app_database(
        compose_file,
        env_add,
        postgis=config.native.reset_db.postgis,
    )
    if rc != 0:
        return rc
    rc = run_pg_restore(compose_file, env_add, restore_extras, config=config)
    if rc != 0:
        return rc
    return run_post_db_restore_manage_commands(
        config,
        compose_file=compose_file,
        env_add=env_add,
        env_name=env_name,
    )


def run_compose_metabase_dump_restore(
    config: ProjectConfig,
    *,
    compose_file: str,
    env_add: dict[str, str],
    env_name: str,
    archive_path: Path | None = None,
    extra_pg_restore_args: Sequence[str] | None = None,
) -> int:
    """Drop/create metabase DB, pg_restore, then metabase post hooks."""
    if not config.has_metabase_fetch():
        return 0
    dump_path = archive_path or config.fetch_metabase_db_dump_path
    if dump_path is None:
        return 0
    extras = list(extra_pg_restore_args or [])
    extras = ["--file", str(dump_path), *extras]
    restore_extras = compose_pg_restore_extras_for_config(
        config,
        extras,
        postgis=False,
    )
    rc = ensure_db_service_running(
        compose_file, env_add, config=config, dk_env_name=env_name
    )
    if rc != 0:
        return rc
    print(
        "db restore: replacing Metabase database with an empty database before restore …",
        file=sys.stderr,
    )
    rc = run_drop_create_metabase_database(compose_file, env_add)
    if rc != 0:
        return rc
    rc = run_pg_restore(
        compose_file,
        env_add,
        restore_extras,
        config=config,
        target="metabase",
    )
    if rc != 0:
        return rc
    return run_post_metabase_db_restore_manage_commands(
        config,
        compose_file=compose_file,
        env_add=env_add,
        env_name=env_name,
    )


def _ensure_local_dumps(config: ProjectConfig, *, auto_fetch: bool) -> int:
    if configured_app_dump_exists(config):
        if config.has_metabase_fetch() and not configured_metabase_dump_exists(config):
            if auto_fetch:
                print("db restore: fetching missing Metabase dump …", file=sys.stderr)
                try:
                    run_fetch_all_dbs(config, only="metabase")
                except SystemExit as exc:
                    return int(exc.code or 1)
            elif not configured_metabase_dump_exists(config):
                print(
                    "db restore: Metabase dump missing — run `uv run dk fetch db --only metabase` "
                    "or pass `--dumps` only when all configured dumps exist.",
                    file=sys.stderr,
                )
                return 1
        return 0
    if not auto_fetch:
        print(
            f"db restore: no dump at {config.fetch_db_dump_path} — run `uv run dk fetch db` first.",
            file=sys.stderr,
        )
        return 1
    print("db restore: fetching configured databases …", file=sys.stderr)
    try:
        run_fetch_all_dbs(config)
    except SystemExit as exc:
        return int(exc.code or 1)
    if not configured_app_dump_exists(config):
        print("db restore: fetch completed but app dump is still missing or too small.", file=sys.stderr)
        return 1
    return 0


def run_unified_db_restore(
    config: ProjectConfig,
    *,
    compose_file: str,
    env_add: dict[str, str],
    env_name: str,
    force_dumps: bool = False,
    dry_run: bool = False,
    skip_confirm: bool = False,
    extra_pgbackrest_args: Sequence[str] | None = None,
    extra_pg_restore_args: Sequence[str] | None = None,
) -> int:
    """Restore local env DB from pgBackRest or configured custom-format dumps."""
    restore_extra = list(extra_pgbackrest_args or [])
    while restore_extra and restore_extra[0] == "--":
        restore_extra.pop(0)

    if force_dumps:
        rc = _ensure_local_dumps(config, auto_fetch=False)
        if rc != 0:
            return rc
        if dry_run:
            print(
                f"dry-run: would restore app DB from {config.fetch_db_dump_path}",
                file=sys.stderr,
            )
            if config.has_metabase_fetch():
                print(
                    f"dry-run: would restore Metabase DB from {config.fetch_metabase_db_dump_path}",
                    file=sys.stderr,
                )
            return 0
        rc = run_compose_app_dump_restore(
            config,
            compose_file=compose_file,
            env_add=env_add,
            env_name=env_name,
            extra_pg_restore_args=extra_pg_restore_args,
        )
        if rc != 0:
            return rc
        return run_compose_metabase_dump_restore(
            config,
            compose_file=compose_file,
            env_add=env_add,
            env_name=env_name,
            extra_pg_restore_args=extra_pg_restore_args,
        )

    if pgbackrest_restore_configured(env_add):
        if dry_run:
            return plan_restore_offline(
                env_add,
                compose_file=compose_file,
                env_name=env_name,
                extra_pgbackrest_args=restore_extra,
                config=config,
                docker_host=str(env_add.get("DOCKER_HOST", "")),
            )
        if not skip_confirm and not sys.stdin.isatty():
            print(
                "Refusing restore without a TTY. Pass --yes if you intend to run non-interactive.",
                file=sys.stderr,
            )
            return 1
        return run_restore_offline(
            env_add,
            compose_file=compose_file,
            env_name=env_name,
            skip_confirm=skip_confirm,
            extra_pgbackrest_args=restore_extra,
            config=config,
        )

    if configured_app_dump_exists(config):
        if dry_run:
            print(
                f"dry-run: would restore app DB from {config.fetch_db_dump_path}",
                file=sys.stderr,
            )
            if config.has_metabase_fetch():
                print(
                    f"dry-run: would restore Metabase DB from {config.fetch_metabase_db_dump_path}",
                    file=sys.stderr,
                )
            return 0
        rc = run_compose_app_dump_restore(
            config,
            compose_file=compose_file,
            env_add=env_add,
            env_name=env_name,
            extra_pg_restore_args=extra_pg_restore_args,
        )
        if rc != 0:
            return rc
        return run_compose_metabase_dump_restore(
            config,
            compose_file=compose_file,
            env_add=env_add,
            env_name=env_name,
            extra_pg_restore_args=extra_pg_restore_args,
        )

    rc = _ensure_local_dumps(config, auto_fetch=True)
    if rc != 0:
        return rc
    if dry_run:
        print(
            f"dry-run: would restore app DB from {config.fetch_db_dump_path} after fetch",
            file=sys.stderr,
        )
        return 0
    rc = run_compose_app_dump_restore(
        config,
        compose_file=compose_file,
        env_add=env_add,
        env_name=env_name,
        extra_pg_restore_args=extra_pg_restore_args,
    )
    if rc != 0:
        return rc
    return run_compose_metabase_dump_restore(
        config,
        compose_file=compose_file,
        env_add=env_add,
        env_name=env_name,
        extra_pg_restore_args=extra_pg_restore_args,
    )
