"""Run project-configured Django management commands after a Compose DB restore."""

from __future__ import annotations

import sys

from catalpa_tooling.compose import _compose
from catalpa_tooling.config import ProjectConfig

_ENV_NAME_PLACEHOLDER = "{env_name}"


def _expand_argv(argv: tuple[str, ...], *, env_name: str) -> tuple[str, ...]:
    return tuple(a.replace(_ENV_NAME_PLACEHOLDER, env_name) for a in argv)


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
    """Run ``dev.reset_db.post_manage_commands`` via local ``uv run manage.py`` (host Postgres)."""
    commands = config.dev.reset_db.post_manage_commands
    if not commands:
        return 0

    from catalpa_tooling.dev_cli import _run_uv_manage

    print(
        f"dev reset-db: running {len(commands)} post-restore management command(s)…",
        file=sys.stderr,
    )
    for argv in commands:
        label = " ".join(argv)
        print(f"dev reset-db: manage.py {label}", file=sys.stderr)
        rc = _run_uv_manage(list(argv))
        if rc != 0:
            print(f"dev reset-db: `manage.py {label}` failed (exit {rc}).", file=sys.stderr)
            return rc
    print("dev reset-db: post-restore commands done.", file=sys.stderr)
    return 0
