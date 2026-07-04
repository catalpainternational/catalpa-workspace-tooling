"""``test smoke`` — layered project health checks for Django compose consumer repos."""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request

import yaml

from catalpa_tooling.compose import _compose, _wait_for_web_service
from catalpa_tooling.config import ProjectConfig, resolve_native_db_name
from catalpa_tooling.env_handlers import _ensure_stack_volumes
from catalpa_tooling.managed_deploy_env import ManagedDeployContext, load_managed_deploy_context, resolve_compose_file_from_info
from catalpa_tooling.pgbackrest_db import db_service_responds
from catalpa_tooling.pgbackrest_volume_config import materialize_configs, postgres_image_from_env
from catalpa_tooling.remote_deploy import _ensure_local_stack_images_built, _insert_up_build_if_no_registry
from catalpa_tooling.restic_files import resolve_env_with_compose_project
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.site_origin import primary_site_origin_from_info


def _load_env_info(config: ProjectConfig, env_name: str) -> dict:
    info_path = config.deploy_envs_dir / env_name / "info.yaml"
    if not info_path.is_file():
        print(f"Missing {info_path}", file=sys.stderr)
        return {}
    with open(info_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return raw if isinstance(raw, dict) else {}


def _resolve_deploy_context(
    config: ProjectConfig, env_name: str
) -> tuple[str, dict[str, str], str, ManagedDeployContext, dict] | None:
    info = _load_env_info(config, env_name)
    if not info:
        return None
    compose_file = resolve_compose_file_from_info(info, config)
    if compose_file is None:
        return None
    ctx = load_managed_deploy_context(
        config,
        env_name,
        info=info,
        compose_file=compose_file,
    )
    if ctx is None:
        return None
    env_add = resolve_env_with_compose_project(
        compose_file,
        ctx.env_add,
        config=config,
        dk_env_name=env_name,
    )
    site_origin = primary_site_origin_from_info(info) or ctx.site_origin
    return compose_file, env_add, site_origin, ctx, info


def _prepare_compose_up(
    ctx: ManagedDeployContext,
    info: dict,
    env_add: dict[str, str],
) -> int:
    """Match ``dk <env> up`` preflight: volumes, local builds, pgBackRest config materialization."""
    rc = _ensure_stack_volumes(
        ctx.config,
        ctx.env_name,
        info,
        env_add,
        ctx.storage_volumes,
        dry_run=False,
    )
    if rc != 0:
        return rc
    rc = _ensure_local_stack_images_built(
        ctx.config,
        env_add,
        use_prepulled_registry=ctx.use_prepulled_registry,
    )
    if rc != 0:
        return rc
    return materialize_configs(
        env_add,
        dry_run=False,
        postgres_image=postgres_image_from_env(env_add, config=ctx.config),
        config=ctx.config,
    )


def _run_compose_manage(
    compose_file: str,
    config: ProjectConfig,
    env_add: dict[str, str],
    *manage_argv: str,
    extra_exec_env: dict[str, str] | None = None,
) -> int:
    cmd = ["exec", "-T"]
    if extra_exec_env:
        for key, value in extra_exec_env.items():
            cmd.extend(["-e", f"{key}={value}"])
    cmd.extend([config.stack_service("web"), "./manage.py", *manage_argv])
    return _compose(compose_file, *cmd, env_add=env_add, check=False).returncode


def _wait_for_db(compose_file: str, config: ProjectConfig, env_add: dict[str, str], *, timeout_seconds: int = 120) -> bool:
    import time

    deadline = time.monotonic() + timeout_seconds
    db_service = config.stack_service("db")
    while time.monotonic() < deadline:
        if db_service_responds(compose_file, env_add):
            r = _compose(
                compose_file,
                "exec",
                "-T",
                db_service,
                "pg_isready",
                "-U",
                "postgres",
                env_add=env_add,
                check=False,
                print_cmd=False,
            )
            if r.returncode == 0:
                return True
        time.sleep(2)
    return False


def _db_name(env_add: dict[str, str], config: ProjectConfig) -> str:
    for key in config.native.reset_db.db_name_env:
        val = (env_add.get(key) or "").strip()
        if val:
            return val
    return resolve_native_db_name(config)


def _db_user(env_add: dict[str, str], config: ProjectConfig) -> str:
    for key in config.native.reset_db.user_env:
        val = (env_add.get(key) or "").strip()
        if val:
            return val
    return config.meta.name


def _run_psql(
    compose_file: str,
    config: ProjectConfig,
    env_add: dict[str, str],
    sql: str,
) -> int:
    db_service = config.stack_service("db")
    return _compose(
        compose_file,
        "exec",
        "-T",
        db_service,
        "psql",
        "-U",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        sql,
        env_add=env_add,
        check=False,
    ).returncode


def _fresh_db_smoke(
    compose_file: str,
    config: ProjectConfig,
    env_add: dict[str, str],
    *,
    check_only: bool,
) -> int:
    primary = _db_name(env_add, config)
    ephemeral = f"{primary}_smoke_empty"
    owner = _db_user(env_add, config)
    print(f"smoke: fresh-db migrate on ephemeral database {ephemeral!r}", file=sys.stderr)

    terminate_sql = (
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{ephemeral}' AND pid <> pg_backend_pid();"
    )
    for sql in (
        terminate_sql,
        f"DROP DATABASE IF EXISTS {ephemeral};",
        f"CREATE DATABASE {ephemeral} OWNER {owner};",
    ):
        if _run_psql(compose_file, config, env_add, sql) != 0:
            print("smoke: fresh-db setup failed", file=sys.stderr)
            return 1

    db_override = {"DJANGO_DB": ephemeral, "POSTGRES_DB": ephemeral}
    migrate_argv = ("migrate", "--check") if check_only else ("migrate", "--noinput")
    if _run_compose_manage(
        compose_file,
        config,
        env_add,
        *migrate_argv,
        extra_exec_env=db_override,
    ) != 0:
        print("smoke: fresh-db migrate failed", file=sys.stderr)
        _run_psql(compose_file, config, env_add, f"DROP DATABASE IF EXISTS {ephemeral};")
        return 1

    if not check_only and _run_compose_manage(
        compose_file,
        config,
        env_add,
        "check",
        extra_exec_env=db_override,
    ) != 0:
        print("smoke: fresh-db manage check failed", file=sys.stderr)
        _run_psql(compose_file, config, env_add, f"DROP DATABASE IF EXISTS {ephemeral};")
        return 1

    if _run_psql(compose_file, config, env_add, f"DROP DATABASE IF EXISTS {ephemeral};") != 0:
        print("smoke: fresh-db cleanup failed", file=sys.stderr)
        return 1
    return 0


def _http_get_ok(url: str, *, timeout: float = 10.0) -> bool:
    ok, _ = _http_get_detail(url, timeout=timeout)
    return ok


def _http_get_detail(url: str, *, timeout: float = 10.0) -> tuple[bool, str | None]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            ok = 200 <= resp.status < 400
            if not ok:
                return False, f"status_{resp.status}"
            return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}:{exc!s}"[:200]


def _wait_for_frontend_url(
    url: str,
    *,
    timeout_seconds: int = 120,
    poll_interval: int = 3,
    request_timeout: float = 60.0,
) -> bool:
    """Poll ``site_origin`` until HTTP 2xx/3xx (webpack dev server blocks until first compile)."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        ok, _ = _http_get_detail(url, timeout=request_timeout)
        if ok:
            return True
        time.sleep(poll_interval)
    return False


def _run_pytest_smoke(config: ProjectConfig, *, fe_url: str, extra_pytest: list[str]) -> int:
    smoke_dir = config.frontend_dir / "smoke"
    if not smoke_dir.is_dir():
        print(f"smoke: missing {smoke_dir} (Playwright smoke tests under paths.frontend/smoke)", file=sys.stderr)
        return 1
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["SMOKE_FE_URL"] = fe_url
    cmd = [
        "uv",
        "run",
        "--group",
        "smoke",
        "pytest",
        str(smoke_dir),
        *extra_pytest,
    ]
    return run_cmd(cmd, cwd=config.repo_root, env=env, check=False).returncode


def run_smoke(
    config: ProjectConfig,
    *,
    env_name: str = "dev",
    no_up: bool = False,
    check_only: bool = False,
    fresh_db: bool = False,
    ci_mode: bool = False,
    pytest_args: list[str] | None = None,
) -> int:
    """Run layered smoke checks. Returns process exit code."""
    if ci_mode and fresh_db:
        print("smoke: ignoring --fresh-db in CI mode (primary DB is already empty)", file=sys.stderr)
        fresh_db = False

    resolved = _resolve_deploy_context(config, env_name)
    if resolved is None:
        return 1
    compose_file, env_add, site_origin, deploy_ctx, info = resolved
    fe_url = (site_origin or "http://127.0.0.1:9011").rstrip("/") + "/"

    if not no_up:
        print(f"smoke: starting stack ({compose_file})", file=sys.stderr)
        if _prepare_compose_up(deploy_ctx, info, env_add) != 0:
            print("smoke: stack prepare failed", file=sys.stderr)
            return 1
        compose_argv = _insert_up_build_if_no_registry(
            ["up", "-d"],
            use_prepulled_registry=deploy_ctx.use_prepulled_registry,
        )
        if _compose(compose_file, *compose_argv, env_add=env_add, check=False).returncode != 0:
            print("smoke: docker compose up failed", file=sys.stderr)
            return 1

    if not _wait_for_db(compose_file, config, env_add):
        print("smoke: database service did not become ready", file=sys.stderr)
        return 1

    if fresh_db:
        rc = _fresh_db_smoke(compose_file, config, env_add, check_only=check_only)
        if rc != 0:
            return rc

    migrate_argv = ("migrate", "--check") if check_only else ("migrate", "--noinput")
    print(f"smoke: primary DB {migrate_argv}", file=sys.stderr)
    if _run_compose_manage(compose_file, config, env_add, *migrate_argv) != 0:
        print("smoke: primary database migrate failed", file=sys.stderr)
        return 1

    print("smoke: manage check", file=sys.stderr)
    if _run_compose_manage(compose_file, config, env_add, "check") != 0:
        print("smoke: manage check failed", file=sys.stderr)
        return 1

    print("smoke: makemigrations --check --dry-run", file=sys.stderr)
    if _run_compose_manage(
        compose_file,
        config,
        env_add,
        "makemigrations",
        "--check",
        "--dry-run",
    ) != 0:
        print("smoke: makemigrations --check failed", file=sys.stderr)
        return 1

    print("smoke: waiting for web service", file=sys.stderr)
    if not _wait_for_web_service(compose_file, config, env_add=env_add):
        print("smoke: web service healthcheck timed out", file=sys.stderr)
        return 1

    print(f"smoke: waiting for frontend URL {fe_url}", file=sys.stderr)
    if not _wait_for_frontend_url(fe_url):
        print(f"smoke: frontend URL did not respond: {fe_url}", file=sys.stderr)
        return 1

    print("smoke: pytest (Playwright PWA load)", file=sys.stderr)
    return _run_pytest_smoke(config, fe_url=fe_url.rstrip("/"), extra_pytest=list(pytest_args or []))
