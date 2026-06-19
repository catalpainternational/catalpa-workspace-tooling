"""Run project-configured Django management commands after a Compose DB restore."""

from __future__ import annotations

import sys
from typing import Any

from catalpa_tooling.compose import _compose
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.host_storage import ensure_host_storage
from catalpa_tooling.pgbackrest_volume_config import ensure_external_stack_volumes
from catalpa_tooling.storage_config import (
    StorageConfigError,
    parse_storage_volumes_from_info,
)

_ENV_NAME_PLACEHOLDER = "{env_name}"


def _expand_argv(argv: tuple[str, ...], *, env_name: str) -> tuple[str, ...]:
    return tuple(a.replace(_ENV_NAME_PLACEHOLDER, env_name) for a in argv)


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


def run_post_db_restore_manage_commands(
    config: ProjectConfig,
    *,
    compose_file: str,
    env_add: dict[str, str],
    env_name: str,
    dry_run: bool = False,
) -> int:
    """Run ``ops.post_db_restore.manage_commands`` when configured for ``env_name``.

    Brings the web service up (``compose up -d --wait``, including compose dependencies),
    then runs each command via ``docker compose exec <web> ./manage.py …``.
    Returns 0 when there is nothing to run.
    """
    hooks = config.ops.post_db_restore
    if not hooks.manage_commands:
        return 0
    if not hooks.applies_to_env(env_name):
        return 0

    web_svc = config.stack_service("web")
    commands = [_expand_argv(argv, env_name=env_name) for argv in hooks.manage_commands]

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
                f"dry-run: would docker compose -f {compose_file} exec -T {web_svc} "
                f"./manage.py {' '.join(argv)}",
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
            "exec",
            "-T",
            web_svc,
            "./manage.py",
            *argv,
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
