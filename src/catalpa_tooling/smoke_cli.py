"""``tests smoke`` — layered project health checks for Django compose consumer repos."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

import yaml

from catalpa_tooling.compose import _compose, _wait_for_web_service
from catalpa_tooling.config import (
    ProjectConfig,
    resolve_frontend_package_manager,
    resolve_native_db_name,
)
from catalpa_tooling.env_handlers import _ensure_stack_volumes
from catalpa_tooling.dc_backup.hosts import (
    dc_backup_tls_extra_compose_files,
    merge_extra_compose_files,
)
from catalpa_tooling.local_proxy import (
    LocalProxyConfigError,
    local_proxy_extra_compose_files,
    sync_local_proxy_for_compose_action,
)
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
    # Host/direnv often has DJANGO_DB from project *.env even when credentials omit it.
    for key in config.native.reset_db.db_name_env:
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return resolve_native_db_name(config)


def _is_cluster_superuser_key(key: str, value: str) -> bool:
    """``POSTGRES_USER=postgres`` is the image superuser, not the Django app role."""
    return key == "POSTGRES_USER" and value == "postgres"


def _lookup_db_user(env: dict[str, str], config: ProjectConfig) -> str | None:
    for key in config.native.reset_db.user_env:
        val = (env.get(key) or "").strip()
        if val and not _is_cluster_superuser_key(key, val):
            return val
    return None


def _compose_printenv(
    compose_file: str,
    config: ProjectConfig,
    env_add: dict[str, str],
    service: str,
    key: str,
) -> str | None:
    r = _compose(
        compose_file,
        "exec",
        "-T",
        service,
        "printenv",
        key,
        env_add=env_add,
        check=False,
        print_cmd=False,
        capture_output=True,
    )
    if r.returncode != 0:
        return None
    val = (getattr(r, "stdout", None) or "").strip()
    return val or None


def _psql_scalar(
    compose_file: str,
    config: ProjectConfig,
    env_add: dict[str, str],
    sql: str,
) -> str | None:
    db_service = config.stack_service("db")
    r = _compose(
        compose_file,
        "exec",
        "-T",
        db_service,
        "psql",
        "-U",
        "postgres",
        "-v",
        "ON_ERROR_STOP=1",
        "-tA",
        "-c",
        sql,
        env_add=env_add,
        check=False,
        print_cmd=False,
        capture_output=True,
    )
    if r.returncode != 0:
        return None
    val = (getattr(r, "stdout", None) or "").strip()
    return val or None


def _pg_ident(name: str) -> str:
    """Return ``name`` if it is a safe unquoted Postgres identifier."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"unsafe postgres identifier: {name!r}")
    return name


def _resolve_db_owner(
    compose_file: str,
    config: ProjectConfig,
    env_add: dict[str, str],
    *,
    primary_db: str,
) -> str | None:
    """Resolve the app DB role for ``CREATE DATABASE … OWNER``.

    ``DJANGO_DB_USER`` usually lives in compose ``env_file`` / container env, not in
    ``info.yaml`` / credentials ``env_add``. Falling back to ``project.name`` (e.g.
    ``catalpa_bero``) creates a non-existent role — probe the stack instead.
    """
    owner = _lookup_db_user(env_add, config)
    if owner:
        return owner
    owner = _lookup_db_user(dict(os.environ), config)
    if owner:
        return owner

    web = config.stack_service("web")
    db = config.stack_service("db")
    for service in (web, db):
        for key in config.native.reset_db.user_env:
            val = _compose_printenv(compose_file, config, env_add, service, key)
            if val and not _is_cluster_superuser_key(key, val):
                return val

    try:
        primary = _pg_ident(primary_db)
    except ValueError:
        return None
    return _psql_scalar(
        compose_file,
        config,
        env_add,
        f"SELECT pg_catalog.pg_get_userbyid(d.datdba) FROM pg_database d "
        f"WHERE d.datname = '{primary}'",
    )


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
    owner = _resolve_db_owner(compose_file, config, env_add, primary_db=primary)
    if not owner:
        print(
            "smoke: could not resolve DB owner for fresh-db "
            "(set DJANGO_DB_USER in credentials/env, or ensure primary DB exists)",
            file=sys.stderr,
        )
        return 1
    try:
        ephemeral_id = _pg_ident(ephemeral)
        owner_id = _pg_ident(owner)
    except ValueError as exc:
        print(f"smoke: fresh-db {exc}", file=sys.stderr)
        return 1

    print(
        f"smoke: fresh-db migrate on ephemeral database {ephemeral!r} (owner {owner!r})",
        file=sys.stderr,
    )

    terminate_sql = (
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{ephemeral_id}' AND pid <> pg_backend_pid();"
    )
    for sql in (
        terminate_sql,
        f"DROP DATABASE IF EXISTS {ephemeral_id};",
        f"CREATE DATABASE {ephemeral_id} OWNER {owner_id};",
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
        _run_psql(compose_file, config, env_add, f"DROP DATABASE IF EXISTS {ephemeral_id};")
        return 1

    if not check_only and _run_compose_manage(
        compose_file,
        config,
        env_add,
        "check",
        extra_exec_env=db_override,
    ) != 0:
        print("smoke: fresh-db manage check failed", file=sys.stderr)
        _run_psql(compose_file, config, env_add, f"DROP DATABASE IF EXISTS {ephemeral_id};")
        return 1

    if _run_psql(compose_file, config, env_add, f"DROP DATABASE IF EXISTS {ephemeral_id};") != 0:
        print("smoke: fresh-db cleanup failed", file=sys.stderr)
        return 1
    print("smoke: fresh-db OK", file=sys.stderr)
    return 0


def _http_get_ok(url: str, *, timeout: float = 10.0) -> bool:
    ok, _ = _http_get_detail(url, timeout=timeout)
    return ok


def _smoke_ssl_context():
    """SSL context that trusts the local-proxy CA when available (HTTPS site_origin)."""
    import ssl

    ctx = ssl.create_default_context()
    ca_path = _local_proxy_ca_path()
    if ca_path is not None:
        ctx.load_verify_locations(cafile=str(ca_path))
    return ctx


def _local_proxy_ca_path():
    """Return path to exported local-proxy CA root, exporting it if needed."""
    try:
        from catalpa_tooling.local_proxy import local_proxy_data_dir
        from catalpa_tooling.local_proxy_ca import export_proxy_ca_to_data_dir

        ca_path = local_proxy_data_dir() / "ca-root.crt"
        if not ca_path.is_file():
            exported = export_proxy_ca_to_data_dir()
            if exported is not None:
                ca_path = exported
        if ca_path.is_file():
            return ca_path
    except Exception:
        return None
    return None


def _inject_local_proxy_ca_env(env: dict[str, str]) -> None:
    """Make urllib/requests in smoke pytest trust the local-proxy CA via SSL_CERT_FILE."""
    from pathlib import Path

    ca_path = _local_proxy_ca_path()
    if ca_path is None:
        return
    import ssl

    parts: list[str] = []
    default_ca = ssl.get_default_verify_paths().openssl_cafile
    if default_ca and Path(default_ca).is_file():
        parts.append(Path(default_ca).read_text(encoding="utf-8"))
    else:
        try:
            import certifi

            parts.append(Path(certifi.where()).read_text(encoding="utf-8"))
        except Exception:
            pass
    parts.append(ca_path.read_text(encoding="utf-8"))
    bundle = ca_path.parent / "ca-bundle-with-local.pem"
    bundle.write_text("\n".join(parts), encoding="utf-8")
    env["SSL_CERT_FILE"] = str(bundle)
    env["REQUESTS_CA_BUNDLE"] = str(bundle)


def _http_get_detail(url: str, *, timeout: float = 10.0) -> tuple[bool, str | None]:
    try:
        req = urllib.request.Request(url)
        kwargs: dict = {"timeout": timeout}
        if url.lower().startswith("https://"):
            kwargs["context"] = _smoke_ssl_context()
        with urllib.request.urlopen(req, **kwargs) as resp:
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
    last_err: str | None = None
    while time.monotonic() < deadline:
        ok, err = _http_get_detail(url, timeout=request_timeout)
        if ok:
            return True
        last_err = err
        time.sleep(poll_interval)
    if last_err:
        print(f"smoke: last frontend probe error: {last_err}", file=sys.stderr)
    return False


def _run_pytest_smoke(config: ProjectConfig, *, fe_url: str, extra_pytest: list[str]) -> int:
    smoke_dir = config.frontend_dir / "smoke"
    if not smoke_dir.is_dir():
        print(f"smoke: missing {smoke_dir} (Playwright smoke tests under paths.frontend/smoke)", file=sys.stderr)
        return 1
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["SMOKE_FE_URL"] = fe_url
    _inject_local_proxy_ca_env(env)
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


def _pytest_selects_elearning(pytest_args: list[str]) -> bool:
    """True when pytest args positively select the ``elearning`` marker (implies functional)."""
    args = list(pytest_args)
    for i, arg in enumerate(args):
        if arg == "-m" and i + 1 < len(args):
            expr = args[i + 1]
            break
        if arg.startswith("-m="):
            expr = arg.split("=", 1)[1]
            break
    else:
        return False
    # Exclude ``-m "not elearning"`` (guest-only); require a positive elearning token.
    if re.search(r"\bnot\s+elearning\b", expr):
        return False
    return bool(re.search(r"(^|[\s(])elearning([\s)]|$)", expr))


def _resolve_functional(
    *,
    functional: bool,
    pytest_args: list[str],
    log_prefix: str = "smoke",
) -> bool:
    if functional:
        return True
    if _pytest_selects_elearning(pytest_args):
        print(
            f"{log_prefix}: pytest -m elearning → functional mode (skip CI gate)",
            file=sys.stderr,
        )
        return True
    return False


def functional_pytest_args(
    *,
    headed: bool,
    extra: list[str] | None = None,
    slowmo_ms: int = 250,
    default_marker: str = "elearning",
) -> list[str]:
    """Build pytest argv for ``tests functional`` / ``tests functional headed``."""
    args = list(extra or [])
    has_marker = any(a == "-m" or a.startswith("-m=") for a in args)
    if not has_marker and default_marker:
        args = ["-m", default_marker, *args]
    if headed:
        if "--headed" not in args:
            args.append("--headed")
        if not any(a == "--slowmo" or a.startswith("--slowmo=") for a in args):
            args.append(f"--slowmo={slowmo_ms}")
    return args


def _frontend_package_scripts(frontend_dir) -> dict[str, str]:
    pkg_path = frontend_dir / "package.json"
    if not pkg_path.is_file():
        return {}
    try:
        data = json.loads(pkg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {k: v for k, v in scripts.items() if isinstance(k, str) and isinstance(v, str)}


# Compose service that holds frontend deps (bero ``compose.dev.yaml`` ``node``).
_DEFAULT_NODE_SERVICE = "node"


def _compose_service_names(
    compose_file: str,
    env_add: dict[str, str],
) -> frozenset[str]:
    r = _compose(
        compose_file,
        "config",
        "--services",
        env_add=env_add,
        check=False,
        print_cmd=False,
        capture_output=True,
    )
    if r.returncode != 0 or not r.stdout:
        return frozenset()
    return frozenset(line.strip() for line in r.stdout.splitlines() if line.strip())


def _run_frontend_script_in_compose(
    compose_file: str,
    env_add: dict[str, str],
    *,
    service: str,
    package_manager: str,
    script: str,
) -> int:
    """One-off ``compose run`` so we reuse image ``node_modules`` (no host install)."""
    return _compose(
        compose_file,
        "run",
        "--rm",
        "-T",
        "--no-deps",
        service,
        package_manager,
        "run",
        script,
        env_add=env_add,
        check=False,
    ).returncode


def _run_frontend_build(
    config: ProjectConfig,
    *,
    compose_file: str | None = None,
    env_add: dict[str, str] | None = None,
) -> int:
    """Run frontend type-check (when present) then production ``build``.

    Prefer ``docker compose run`` on the ``node`` service when that service exists in
    the compose file (image already has ``node_modules``). Fall back to a host
    package-manager run for native setups without a node service.

    Bero's webpack uses ``transpileOnly`` + ForkTsChecker; a bare ``webpack --mode=production``
    can exit 0 despite TypeScript errors. Prefer an explicit ``type-check`` script
    (``tsc --noEmit``), then ``build`` (which may also chain type-check).
    """
    from catalpa_tooling.native_cli import _run_pkg_script

    frontend_dir = config.frontend_dir
    scripts = _frontend_package_scripts(frontend_dir)
    if not scripts.get("build") and not scripts.get("type-check"):
        print(
            f"smoke: no package.json type-check/build script under {frontend_dir}; skipping",
            file=sys.stderr,
        )
        return 0

    frontend_cfg = config.native.frontend
    package_manager = resolve_frontend_package_manager(
        frontend_dir, configured=frontend_cfg.package_manager
    )
    node_version = frontend_cfg.node_version

    use_compose = False
    node_service = _DEFAULT_NODE_SERVICE
    if compose_file is not None and env_add is not None:
        if node_service in _compose_service_names(compose_file, env_add):
            use_compose = True
        else:
            print(
                f"smoke: no compose service {node_service!r}; "
                f"falling back to host {package_manager}",
                file=sys.stderr,
            )

    def _run_script(script: str) -> int:
        if use_compose:
            assert compose_file is not None and env_add is not None
            return _run_frontend_script_in_compose(
                compose_file,
                env_add,
                service=node_service,
                package_manager=package_manager,
                script=script,
            )
        return _run_pkg_script(
            script,
            frontend_dir,
            package_manager,
            node_version=node_version,
        )

    # Explicit type-check first so failures are obvious before a long webpack build.
    # Skip when ``build`` already starts with type-check to avoid running tsc twice.
    build_script = scripts.get("build", "")
    build_chains_typecheck = bool(
        scripts.get("type-check")
        and re.search(r"(^|&&|\s)(pnpm|npm|yarn)\s+run\s+type-check\b", build_script)
    )
    where = f"compose:{node_service}" if use_compose else "host"
    if scripts.get("type-check") and not build_chains_typecheck:
        print(
            f"smoke: frontend type-check ({where} {package_manager} run type-check)",
            file=sys.stderr,
        )
        rc = _run_script("type-check")
        if rc != 0:
            return rc

    if scripts.get("build"):
        label = "type-check + webpack" if build_chains_typecheck else "webpack"
        print(
            f"smoke: frontend production build "
            f"({where} {package_manager} run build — {label})",
            file=sys.stderr,
        )
        return _run_script("build")
    return 0


def run_smoke(
    config: ProjectConfig,
    *,
    env_name: str = "dev",
    no_up: bool = False,
    check_only: bool = False,
    functional: bool = False,
    pytest_args: list[str] | None = None,
    log_prefix: str = "smoke",
) -> int:
    """Run layered CI gate or functional Playwright checks. Returns process exit code.

    CI gate (``functional=False``): empty-DB migrate on ephemeral ``{dbname}_smoke_empty``
    (never touches the primary DB), ``makemigrations --check``, frontend production build,
    HTTP wait, guest pytest.

    Functional (``functional=True``): skip the gate; wait for HTTP and run pytest only.
    """
    pytest_args = list(pytest_args or [])
    functional = _resolve_functional(
        functional=functional,
        pytest_args=pytest_args,
        log_prefix=log_prefix,
    )
    p = log_prefix

    if functional and check_only:
        print(
            f"{p}: functional mode ignores --check-only",
            file=sys.stderr,
        )

    resolved = _resolve_deploy_context(config, env_name)
    if resolved is None:
        return 1
    compose_file, env_add, site_origin, deploy_ctx, info = resolved
    fe_url = (site_origin or "http://127.0.0.1:9011").rstrip("/") + "/"

    if not no_up:
        print(f"{p}: starting stack ({compose_file})", file=sys.stderr)
        if _prepare_compose_up(deploy_ctx, info, env_add) != 0:
            print(f"{p}: stack prepare failed", file=sys.stderr)
            return 1
        compose_argv = _insert_up_build_if_no_registry(
            ["up", "-d"],
            use_prepulled_registry=deploy_ctx.use_prepulled_registry,
        )
        # Match ``dk <env> up``: ensure shared local proxy + drop host :80/:443 publishing.
        try:
            proxy_rc = sync_local_proxy_for_compose_action(
                info,
                config,
                env_name,
                compose_argv,
                env_add,
            )
            if proxy_rc != 0:
                print(f"{p}: local proxy sync failed", file=sys.stderr)
                return proxy_rc
            extra_compose_files = merge_extra_compose_files(
                local_proxy_extra_compose_files(
                    info,
                    config,
                    env_name,
                    env_add,
                    compose_argv,
                ),
                dc_backup_tls_extra_compose_files(
                    info,
                    config,
                    env_name,
                    env_add,
                    compose_argv,
                ),
            )
        except (LocalProxyConfigError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 1
        if (
            _compose(
                compose_file,
                *compose_argv,
                env_add=env_add,
                extra_compose_files=extra_compose_files,
                check=False,
            ).returncode
            != 0
        ):
            print(f"{p}: docker compose up failed", file=sys.stderr)
            return 1

    if not functional:
        if not _wait_for_db(compose_file, config, env_add):
            print(f"{p}: database service did not become ready", file=sys.stderr)
            return 1

        rc = _fresh_db_smoke(compose_file, config, env_add, check_only=check_only)
        if rc != 0:
            return rc

        print(f"{p}: makemigrations --check --dry-run", file=sys.stderr)
        if _run_compose_manage(
            compose_file,
            config,
            env_add,
            "makemigrations",
            "--check",
            "--dry-run",
        ) != 0:
            print(f"{p}: makemigrations --check failed", file=sys.stderr)
            return 1

        if _run_frontend_build(config, compose_file=compose_file, env_add=env_add) != 0:
            print(f"{p}: frontend production build failed", file=sys.stderr)
            return 1

    print(f"{p}: waiting for web service", file=sys.stderr)
    if not _wait_for_web_service(compose_file, config, env_add=env_add):
        print(f"{p}: web service healthcheck timed out", file=sys.stderr)
        return 1

    print(f"{p}: waiting for frontend URL {fe_url}", file=sys.stderr)
    if not _wait_for_frontend_url(fe_url):
        print(f"{p}: frontend URL did not respond: {fe_url}", file=sys.stderr)
        return 1

    label = "functional pytest" if functional else "pytest (guest)"
    print(f"{p}: {label}", file=sys.stderr)
    return _run_pytest_smoke(config, fe_url=fe_url.rstrip("/"), extra_pytest=pytest_args)
