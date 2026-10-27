"""Run project-configured hooks after a Compose DB restore."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

from catalpa_tooling.compose import _compose, manage_exec_args
from catalpa_tooling.config import DbPsqlRestoreEntry, ProjectConfig
from catalpa_tooling.host_storage import ensure_host_storage
from catalpa_tooling.pgbackrest_volume_config import ensure_external_stack_volumes
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.storage_config import (
    StorageConfigError,
    parse_storage_volumes_from_info,
)

# Placeholders that need SITE_ORIGIN; a hook using one refuses to run without it.
_SITE_PLACEHOLDERS = ("{site_origin}", "{site_host}")


def _hook_placeholders(
    config: ProjectConfig,
    *,
    env_name: str,
    env_add: dict[str, str],
) -> dict[str, str]:
    """Values for ``{env_name}``, ``{site_origin}``, ``{site_host}``, ``{worktree_slug}``.

    ``{site_origin}`` comes from ``SITE_ORIGIN`` in the resolved compose env, which in a
    worktree is already the overlay origin. ``{worktree_slug}`` is empty outside a worktree,
    and for envs other than the worktree's base env.
    """
    from catalpa_tooling.site_origin import hostnames_from_origins
    from catalpa_tooling.worktree_overlay import WorktreeOverlayError, load_worktree_overlay

    values = {"{env_name}": env_name}
    origin = str(env_add.get("SITE_ORIGIN") or "").strip()
    if origin:
        values["{site_origin}"] = origin
        values["{site_host}"] = hostnames_from_origins([origin])[0]
    slug = ""
    try:
        overlay = load_worktree_overlay(config.repo_root)
    except WorktreeOverlayError:
        overlay = None
    if overlay is not None and overlay.base_env == env_name:
        slug = overlay.slug
    values["{worktree_slug}"] = slug
    return values


def _expand_argv(argv: tuple[str, ...], values: dict[str, str]) -> tuple[str, ...]:
    out = []
    for a in argv:
        for key, value in values.items():
            a = a.replace(key, value)
        out.append(a)
    return tuple(out)


def _expand_commands(
    config: ProjectConfig,
    commands: Sequence[tuple[str, ...]],
    *,
    env_name: str,
    env_add: dict[str, str],
    hook_label: str,
) -> list[tuple[str, ...]] | None:
    """Expand placeholders in ``commands``; None (after printing why) when one can't be filled."""
    values = _hook_placeholders(config, env_name=env_name, env_add=env_add)
    for argv in commands:
        missing = [p for p in _SITE_PLACEHOLDERS if p not in values and any(p in a for a in argv)]
        if missing:
            print(
                f"{hook_label}: `manage.py {' '.join(argv)}` uses {', '.join(missing)}, "
                f"but SITE_ORIGIN is not set for env {env_name!r}.",
                file=sys.stderr,
            )
            return None
    return [_expand_argv(argv, values) for argv in commands]


def post_db_restore_applies(config: ProjectConfig, env_name: str) -> bool:
    """True when ``ops.post_db_restore`` has hooks that run for ``env_name``."""
    hooks = config.ops.post_db_restore
    return bool(hooks.manage_commands or hooks.db_psql) and hooks.applies_to_env(env_name)


def _load_env_info(config: ProjectConfig, env_name: str) -> dict[str, Any]:
    info_path = config.deploy_envs_dir / env_name / "info.yaml"
    if not info_path.is_file():
        return {}
    import yaml

    with open(info_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return raw if isinstance(raw, dict) else {}


def _ensure_stack_volumes(
    config: ProjectConfig,
    env_name: str,
    env_add: dict[str, str],
    *,
    dry_run: bool = False,
) -> int:
    """Ensure compose stack volumes exist before bringing the web service up."""
    info = _load_env_info(config, env_name)
    try:
        storage_volumes = parse_storage_volumes_from_info(info, config)
    except StorageConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if storage_volumes:
        return ensure_host_storage(
            config,
            env_name,
            info,
            storage_volumes,
            env_add=env_add,
            dry_run=dry_run,
        )
    return ensure_external_stack_volumes(env_add, dry_run=dry_run, config=config)


def _resolve_db_psql_container_path(
    config: ProjectConfig,
    entry: DbPsqlRestoreEntry,
    *,
    compose_file: str,
    env_add: dict[str, str],
    hook_label: str,
) -> tuple[int, str | None]:
    """Return ``(exit_code, container_path)``; copy repo-relative files into ``db`` first."""
    from catalpa_tooling.pgbackrest_db import _merged_process_env

    file_spec = entry.file
    if file_spec.startswith("/"):
        return 0, file_spec

    host_path = (config.repo_root / file_spec).resolve()
    if not host_path.is_file():
        print(
            f"{hook_label}: db_psql file not found: {host_path}",
            file=sys.stderr,
        )
        return 1, None

    prefix = config.ops.pgbackrest.restore_temp_prefix
    container_path = f"/tmp/{prefix}{os.urandom(8).hex()}.sql"
    merged = _merged_process_env(env_add)

    cp = run_cmd(
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "cp",
            str(host_path),
            f"db:{container_path}",
        ],
        env=merged,
        stdin=subprocess.DEVNULL,
        check=False,
        print_cmd=False,
    )
    if cp.returncode != 0:
        print(
            f"{hook_label}: `docker compose cp` failed for {host_path}.",
            file=sys.stderr,
        )
        return cp.returncode, None

    fix = run_cmd(
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "exec",
            "-T",
            "-u",
            "root",
            "db",
            "sh",
            "-c",
            f"chmod 644 {shlex.quote(container_path)} && chown postgres:postgres {shlex.quote(container_path)}",
        ],
        env=merged,
        stdin=subprocess.DEVNULL,
        check=False,
        print_cmd=False,
    )
    if fix.returncode != 0:
        print(
            f"{hook_label}: could not chmod SQL file in `db` container.",
            file=sys.stderr,
        )
        return fix.returncode, None

    return 0, container_path


def run_db_psql_hooks(
    config: ProjectConfig,
    entries: Sequence[DbPsqlRestoreEntry],
    *,
    compose_file: str,
    env_add: dict[str, str],
    hook_label: str,
    dry_run: bool = False,
) -> int:
    """Run ``db_psql`` entries via ``docker compose exec db psql -U postgres``."""
    from catalpa_tooling.pgbackrest_db import DbRestoreTarget, _db_shell_vars, _merged_process_env

    if not entries:
        return 0

    for entry in entries:
        target: DbRestoreTarget = entry.target  # type: ignore[assignment]
        if dry_run:
            print(
                f"dry-run: would {hook_label} db_psql target={entry.target} file={entry.file}",
                file=sys.stderr,
            )
            continue

        rc, container_path = _resolve_db_psql_container_path(
            config,
            entry,
            compose_file=compose_file,
            env_add=env_add,
            hook_label=hook_label,
        )
        if rc != 0:
            return rc
        assert container_path is not None

        inner = (
            _db_shell_vars(target)
            + 'export PGPASSWORD="$POSTGRES_PASSWORD"; '
            + "psql -h 127.0.0.1 -p 5432 -U postgres -d "
            + '"$APP_DB" -v ON_ERROR_STOP=1 -f '
            + shlex.quote(container_path)
        )
        print(
            f"{hook_label}: running db_psql target={entry.target} file={entry.file} …",
            file=sys.stderr,
        )
        r = run_cmd(
            [
                "docker",
                "compose",
                "-f",
                compose_file,
                "exec",
                "-T",
                "db",
                "sh",
                "-c",
                inner,
            ],
            env=_merged_process_env(env_add),
            stdin=subprocess.DEVNULL,
            check=False,
            print_cmd=False,
        )
        if r.returncode != 0:
            print(
                f"{hook_label}: db_psql failed for {entry.file} (exit {r.returncode}).",
                file=sys.stderr,
            )
            return r.returncode

    return 0


def run_post_db_restore_manage_commands(
    config: ProjectConfig,
    *,
    compose_file: str,
    env_add: dict[str, str],
    env_name: str,
    dry_run: bool = False,
) -> int:
    """Run ``ops.post_db_restore`` hooks when configured for ``env_name``.

    ``db_psql`` runs first (Postgres superuser SQL in the ``db`` container), then
    ``manage_commands`` via ``docker compose exec <web> ./manage.py …``.
    Returns 0 when there is nothing to run.
    """
    hooks = config.ops.post_db_restore
    if not post_db_restore_applies(config, env_name):
        return 0

    commands = _expand_commands(
        config,
        hooks.manage_commands,
        env_name=env_name,
        env_add=env_add,
        hook_label="post_db_restore",
    )
    if commands is None:
        return 1

    rc = run_db_psql_hooks(
        config,
        hooks.db_psql,
        compose_file=compose_file,
        env_add=env_add,
        hook_label="post_db_restore",
        dry_run=dry_run,
    )
    if rc != 0:
        return rc

    if not hooks.manage_commands:
        if not dry_run and hooks.db_psql:
            print("post_db_restore: done.", file=sys.stderr)
        return 0

    web_svc = config.stack_service("web")

    rc = _ensure_stack_volumes(config, env_name, env_add, dry_run=dry_run)
    if rc != 0:
        print(
            f"post_db_restore: ensuring stack volumes failed (exit {rc}).",
            file=sys.stderr,
        )
        return rc

    if dry_run:
        print(
            f"dry-run: would docker compose -f {compose_file} up -d {web_svc} --wait",
            file=sys.stderr,
        )
        for argv in commands:
            print(
                f"dry-run: would docker compose -f {compose_file} "
                f"{' '.join(manage_exec_args(config, argv, tty=False))}",
                file=sys.stderr,
            )
        return 0

    print(
        f"post_db_restore: starting `{web_svc}` and running {len(commands)} management command(s)…",
        file=sys.stderr,
    )
    r = _compose(
        compose_file,
        "up",
        "-d",
        web_svc,
        "--wait",
        env_add=env_add,
        check=False,
    )
    if r.returncode != 0:
        print(
            f"post_db_restore: `docker compose up -d {web_svc} --wait` failed (exit {r.returncode}).",
            file=sys.stderr,
        )
        return r.returncode

    for argv in commands:
        label = " ".join(argv)
        print(f"post_db_restore: manage.py {label}", file=sys.stderr)
        r = _compose(
            compose_file,
            *manage_exec_args(config, argv, tty=False),
            env_add=env_add,
            check=False,
        )
        if r.returncode != 0:
            print(
                f"post_db_restore: `manage.py {label}` failed (exit {r.returncode}).",
                file=sys.stderr,
            )
            return r.returncode

    print("post_db_restore: done.", file=sys.stderr)
    return 0


def run_post_metabase_db_restore_manage_commands(
    config: ProjectConfig,
    *,
    compose_file: str,
    env_add: dict[str, str],
    env_name: str,
    dry_run: bool = False,
) -> int:
    """Run ``ops.post_metabase_db_restore`` hooks when configured for ``env_name``."""
    hooks = config.ops.post_metabase_db_restore
    if not hooks.manage_commands and not hooks.restart_services and not hooks.db_psql:
        return 0
    if not hooks.applies_to_env(env_name):
        return 0

    commands = _expand_commands(
        config,
        hooks.manage_commands,
        env_name=env_name,
        env_add=env_add,
        hook_label="post_metabase_db_restore",
    )
    if commands is None:
        return 1

    rc = run_db_psql_hooks(
        config,
        hooks.db_psql,
        compose_file=compose_file,
        env_add=env_add,
        hook_label="post_metabase_db_restore",
        dry_run=dry_run,
    )
    if rc != 0:
        return rc

    web_svc = config.stack_service("web")

    if hooks.manage_commands:
        rc = _ensure_stack_volumes(config, env_name, env_add, dry_run=dry_run)
        if rc != 0:
            print(
                f"post_metabase_db_restore: ensuring stack volumes failed (exit {rc}).",
                file=sys.stderr,
            )
            return rc

    if dry_run:
        if hooks.manage_commands:
            print(
                f"dry-run: would docker compose -f {compose_file} up -d {web_svc} --wait",
                file=sys.stderr,
            )
            for argv in commands:
                print(
                    f"dry-run: would docker compose -f {compose_file} "
                    f"{' '.join(manage_exec_args(config, argv, tty=False))}",
                    file=sys.stderr,
                )
        for svc in hooks.restart_services:
            print(
                f"dry-run: would docker compose -f {compose_file} restart {svc}",
                file=sys.stderr,
            )
        return 0

    if hooks.manage_commands:
        print(
            "post_metabase_db_restore: starting web service and running "
            f"{len(commands)} management command(s)…",
            file=sys.stderr,
        )
        r = _compose(
            compose_file,
            "up",
            "-d",
            web_svc,
            "--wait",
            env_add=env_add,
            check=False,
        )
        if r.returncode != 0:
            print(
                f"post_metabase_db_restore: `docker compose up -d {web_svc} --wait` failed "
                f"(exit {r.returncode}).",
                file=sys.stderr,
            )
            return r.returncode
        for argv in commands:
            label = " ".join(argv)
            print(f"post_metabase_db_restore: manage.py {label}", file=sys.stderr)
            r = _compose(
                compose_file,
                *manage_exec_args(config, argv, tty=False),
                env_add=env_add,
                check=False,
            )
            if r.returncode != 0:
                print(
                    f"post_metabase_db_restore: `manage.py {label}` failed (exit {r.returncode}).",
                    file=sys.stderr,
                )
                return r.returncode

    for svc in hooks.restart_services:
        print(f"post_metabase_db_restore: restarting {svc} …", file=sys.stderr)
        r = _compose(compose_file, "restart", svc, env_add=env_add, check=False)
        if r.returncode != 0:
            print(
                f"post_metabase_db_restore: `docker compose restart {svc}` failed "
                f"(exit {r.returncode}).",
                file=sys.stderr,
            )
            return r.returncode

    if hooks.restart_services or hooks.manage_commands or hooks.db_psql:
        print("post_metabase_db_restore: done.", file=sys.stderr)
    return 0


def run_reset_db_post_manage_commands(config: ProjectConfig) -> int:
    """Run ``native.reset_db.post_manage_commands`` via host ``uv run manage.py`` (Postgres on localhost)."""
    commands = config.native.reset_db.post_manage_commands
    if not commands:
        return 0

    from catalpa_tooling.native_cli import _run_uv_manage

    print(
        f"native reset-db: running {len(commands)} post-restore management command(s)…",
        file=sys.stderr,
    )
    for argv in commands:
        label = " ".join(argv)
        print(f"native reset-db: manage.py {label}", file=sys.stderr)
        rc = _run_uv_manage(list(argv))
        if rc != 0:
            print(f"native reset-db: `manage.py {label}` failed (exit {rc}).", file=sys.stderr)
            return rc
    print("native reset-db: post-restore commands done.", file=sys.stderr)
    return 0
