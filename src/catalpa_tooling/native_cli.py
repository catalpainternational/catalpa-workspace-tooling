"""argparse entrypoint for native: host processes without Docker (Django, Vite, …)."""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv

from catalpa_tooling.cli.completion import activate
from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.deprecation import warn_deprecated
from catalpa_tooling.frontend_pkg import package_install_cmd, package_run_cmd
from catalpa_tooling.native_parser import build_native_parser
from catalpa_tooling.native_start import run_native_start
from catalpa_tooling.fetch_media import run_fetch_media
from catalpa_tooling.fetch_db import run_fetch_all_dbs
from catalpa_tooling.config import (
    DEFAULT_LOCAL_PG_HOST,
    DEFAULT_LOCAL_PG_PORT,
    ProjectConfig,
    resolve_native_db_name,
)

# Custom-format dumps smaller than this are almost certainly stubs (fetch not run).
_MIN_CUSTOM_DUMP_BYTES = 100_000
from catalpa_tooling.post_db_restore import run_reset_db_post_manage_commands
from catalpa_tooling.run_cmd import format_shell_command, run as run_cmd
from catalpa_tooling.script_discovery import (
    reset_db_post_script,
)
from catalpa_tooling.script_runner import run_bash_script

def _config() -> ProjectConfig:
    return ProjectConfig.from_cwd()


def _load_env_local(cfg: ProjectConfig) -> None:
    """Load repo-root ``.env.local`` into ``os.environ`` (from ``tooling.yaml`` ``paths.env_local``).

    Uses ``override=True`` so values from ``paths.env_local`` win over inherited shell env
    (e.g. ``DJANGO_DB_USER`` from a project ``*.env`` loaded by direnv) for native commands only.
    """
    path = cfg.env_local_path
    if path.is_file():
        load_dotenv(path, override=True)


def _django_manage_native_env(cfg: ProjectConfig) -> dict[str, str]:
    """Env merged into every ``uv run ./manage.py …`` invoked by this CLI when keys are unset.

    Loads ``.env.local`` first (when present) so local overrides apply; then sets
    ``DJANGO_DEBUG``, ``EMAIL_BACKEND_FOLDER``, and ``DJANGO_MEDIA_ROOT`` (when
    ``paths.media_dir`` is set) only if missing — same effect as a minimal ``.env.local``
    without requiring that file in every worktree.

    DB host/port/name defaults use the same ``native.reset_db`` env key lists as
    ``native reset-db`` / ``native pg-restore`` so Django and libpq hit the same server.
    """
    _load_env_local(cfg)
    reset = cfg.native.reset_db
    db_default = resolve_native_db_name(cfg)
    extra: dict[str, str] = {}
    if not (os.environ.get("DJANGO_DEBUG") or "").strip():
        extra["DJANGO_DEBUG"] = "1"
    if not (os.environ.get("EMAIL_BACKEND_FOLDER") or "").strip():
        extra["EMAIL_BACKEND_FOLDER"] = str(cfg.email_backend_dir.resolve())
    if cfg.media_dir is not None and not (os.environ.get("DJANGO_MEDIA_ROOT") or "").strip():
        extra["DJANGO_MEDIA_ROOT"] = str(cfg.media_dir.resolve())
    if not _env_first(reset.db_name_env):
        for key in reset.db_name_env:
            extra[key] = db_default
    if not _env_first(reset.host_env):
        for key in reset.host_env:
            extra[key] = DEFAULT_LOCAL_PG_HOST
    if not _env_first(reset.port_env):
        for key in reset.port_env:
            extra[key] = DEFAULT_LOCAL_PG_PORT
    # Omit user/password when unset — Django/psycopg and libpq use the current OS user (Postgres.app).
    return extra


def _env_first(keys: tuple[str, ...]) -> str | None:
    for key in keys:
        val = os.environ.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def _pg_env_for_cli(config: ProjectConfig) -> tuple[str, dict[str, str]]:
    """Database connection for libpq CLI tools from ``paths.env_local`` and ``local.reset_db`` env keys.

    Admin tools (``dropdb``, ``createdb``, ``pg_restore``, ``psql``) connect as the current OS
    user — not ``native.reset_db.user_env`` / compose production roles — so local Postgres.app
    trust auth works without creating app roles first.
    """
    _load_env_local(config)
    reset = config.native.reset_db
    env = os.environ.copy()
    dbname = _env_first(reset.db_name_env) or resolve_native_db_name(config)
    env["PGHOST"] = _env_first(reset.host_env) or DEFAULT_LOCAL_PG_HOST
    env["PGPORT"] = _env_first(reset.port_env) or DEFAULT_LOCAL_PG_PORT
    env.pop("PGUSER", None)
    env.pop("PGPASSWORD", None)
    return dbname, env


def _resolve_reset_dump_path(
    config: ProjectConfig,
    from_dump: Path | None,
    *,
    explicit: bool,
) -> Path | None:
    """Return dump path for ``native reset-db`` (CLI override or non-empty ``paths.fetch_db_dump``)."""
    if from_dump is not None:
        path = from_dump.expanduser().resolve()
        if not path.is_file():
            print(f"native reset-db: not a file: {path}", file=sys.stderr)
            raise SystemExit(1)
        if path.stat().st_size == 0:
            print(f"native reset-db: dump file is empty: {path}", file=sys.stderr)
            raise SystemExit(1)
        return path
    default = config.fetch_db_dump_path
    if default.is_file() and default.stat().st_size > 0:
        return default.resolve()
    if explicit:
        print(
            f"native reset-db: no dump at {default} — run `uv run native fetch db` first "
            "or pass --from-dump PATH.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return None


def _custom_dump_table_count(path: Path) -> int | None:
    """Count TABLE entries in a custom-format archive via ``pg_restore -l``."""
    if not shutil.which("pg_restore"):
        return None
    proc = run_cmd(
        ["pg_restore", "-l", str(path)],
        check=False,
        capture_output=True,
        print_cmd=False,
    )
    if proc.returncode != 0:
        return None
    out = proc.stdout.decode() if proc.stdout else ""
    # TOC data lines: ``219; 1259 29129 TABLE public auth_group …`` (comments start with ``;`` only).
    return sum(
        1
        for line in out.splitlines()
        if " TABLE " in line and not line.lstrip().startswith(";")
    )


def _require_usable_custom_dump(path: Path) -> None:
    """Abort when the dump file is present but too small or has no tables to restore."""
    size = path.stat().st_size
    if size < _MIN_CUSTOM_DUMP_BYTES:
        print(
            f"native reset-db: dump is only {size} bytes (expected a fetched production dump): {path}",
            file=sys.stderr,
        )
        print(
            "  Run `uv run native fetch db` (or `uv run scripts fetch-db`) to download a real dump.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    tables = _custom_dump_table_count(path)
    if tables == 0:
        print(
            f"native reset-db: dump contains no TABLE entries (empty archive): {path}",
            file=sys.stderr,
        )
        print(
            "  Run `uv run native fetch db` to replace it with a production dump.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _public_table_count(dbname: str, env: dict[str, str]) -> int | None:
    """Number of base tables in ``public`` after restore (sanity check)."""
    if not shutil.which("psql"):
        return None
    flags = _pg_conn_flags(env)
    proc = run_cmd(
        [
            "psql",
            *flags,
            "-d",
            dbname,
            "-tAc",
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE';",
        ],
        env=env,
        check=False,
        capture_output=True,
        print_cmd=False,
    )
    if proc.returncode != 0:
        return None
    out = (proc.stdout.decode() if proc.stdout else "").strip()
    try:
        return int(out)
    except ValueError:
        return None


def _pg_conn_flags(env: dict[str, str]) -> list[str]:
    """``-h`` / ``-p`` / ``-U`` for dropdb, createdb, psql."""
    out: list[str] = []
    if h := env.get("PGHOST"):
        out.extend(["-h", h])
    if p := env.get("PGPORT"):
        out.extend(["-p", p])
    if u := env.get("PGUSER"):
        out.extend(["-U", u])
    return out


def _pg_target_line(dbname: str, env: dict[str, str]) -> str:
    """Human-readable connection target (no secrets)."""
    host = env.get("PGHOST") or "(local socket)"
    port = env.get("PGPORT") or "5432"
    user = env.get("PGUSER") or "(default OS user)"
    return f"database={dbname!r} host={host} port={port} user={user}"


def _pg_restore_extras_skip_remote_roles(extras: Sequence[str]) -> list[str]:
    """Prepend ``--no-owner`` / ``--no-acl`` when missing.

    Dumps from staging or Docker often reference roles like ``django_app`` that do not exist on a
    dev machine; without these flags, ``pg_restore`` errors on ``OWNER TO`` / ``GRANT`` statements.
    """
    xs = list(extras)
    if "--no-owner" not in xs:
        xs.insert(0, "--no-owner")
    if "--no-acl" not in xs and "--no-privileges" not in xs:
        xs.insert(0, "--no-acl")
    return xs


def _run_reset_db_drop_create_migrate_seed(
    *,
    from_dump: Path | None = None,
    pg_restore_extras: Sequence[str] | None = None,
    explicit_dump: bool = False,
) -> int:
    """dropdb → createdb → pg_restore (if dump) or PostGIS + migrate/hook; then post_manage_commands."""
    cfg = _config()
    dump_path = _resolve_reset_dump_path(cfg, from_dump, explicit=explicit_dump)
    use_dump = dump_path is not None

    tools = ["dropdb", "createdb"]
    if use_dump:
        tools.append("pg_restore")
    else:
        tools.append("psql")

    for tool in tools:
        if not shutil.which(tool):
            print(
                f"native reset-db: `{tool}` not found on PATH (install PostgreSQL client tools).",
                file=sys.stderr,
            )
            return 127

    post_script, post_script_deprecated = reset_db_post_script(cfg.scripts_dir)
    if post_script_deprecated and post_script is not None:
        warn_deprecated(
            f"{post_script_deprecated}reset-db-post.sh",
            "native-reset-db-post.sh",
            context="scripts",
        )
    env_file = cfg.env_local_path
    if env_file.is_file():
        print(f"native reset-db: loaded {env_file.name} (same as Django settings)", flush=True)

    dbname, env = _pg_env_for_cli(cfg)
    flags = _pg_conn_flags(env)
    reset_cfg = cfg.native.reset_db

    migrate_steps = 0
    if not use_dump:
        if reset_cfg.postgis:
            migrate_steps += 1
        migrate_steps += 1
    total = 2 + (1 if use_dump else migrate_steps)

    print("native reset-db: starting (PostgreSQL client tools)", flush=True)
    print(f"  target: {_pg_target_line(dbname, env)}", flush=True)

    step = 1
    print(
        f"  {step}/{total} dropdb --if-exists: remove existing database if present "
        f"(this deletes all data in {dbname!r})",
        flush=True,
    )
    rc = run_cmd(["dropdb", "--if-exists", *flags, dbname], env=env, check=False).returncode
    if rc != 0:
        print(f"  failed: dropdb exited with {rc}", file=sys.stderr, flush=True)
        return rc
    print("  done: dropdb finished (database removed or did not exist).", flush=True)
    step += 1

    print(f"  {step}/{total} createdb: create empty database {dbname!r}", flush=True)
    rc = run_cmd(["createdb", *flags, dbname], env=env, check=False).returncode
    if rc != 0:
        print(f"  failed: createdb exited with {rc}", file=sys.stderr, flush=True)
        return rc
    print("  done: createdb finished (new empty database).", flush=True)
    step += 1

    if use_dump:
        assert dump_path is not None
        _require_usable_custom_dump(dump_path)
        merged = _pg_restore_extras_skip_remote_roles(
            [*reset_cfg.pg_restore_args, *(pg_restore_extras or ())],
        )
        extras = ["--file", str(dump_path), *merged]
        print(f"  {step}/{total} pg_restore: load dump {dump_path}", flush=True)
        rc = _run_pg_restore(extras, config=cfg)
        if rc != 0:
            print(f"  failed: pg_restore exited with {rc}", file=sys.stderr, flush=True)
            return rc
        print("  done: pg_restore finished.", flush=True)
        tables = _public_table_count(dbname, env)
        if tables == 0:
            print(
                "native reset-db: database has no public tables after pg_restore "
                f"(target {_pg_target_line(dbname, env)}).",
                file=sys.stderr,
            )
            print(
                "  The dump may be wrong or pg_restore failed silently — "
                "re-fetch with `uv run native fetch db` and retry.",
                file=sys.stderr,
            )
            return 1
    else:
        if reset_cfg.postgis:
            print(f"  {step}/{total} psql: CREATE EXTENSION postgis", flush=True)
            rc = run_cmd(
                [
                    "psql",
                    *flags,
                    "-d",
                    dbname,
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-c",
                    "CREATE EXTENSION IF NOT EXISTS postgis;",
                ],
                env=env,
                check=False,
            ).returncode
            if rc != 0:
                print(f"  failed: psql exited with {rc}", file=sys.stderr, flush=True)
                return rc
            print("  done: PostGIS extension ready.", flush=True)
            step += 1

        if post_script is not None:
            print(f"  {step}/{total} bash {post_script.name} (project hook)", flush=True)
            rc = run_bash_script(cfg, post_script, [], label="native reset-db")
            if rc != 0:
                print(f"  failed: {post_script.name} exited with {rc}", file=sys.stderr, flush=True)
                return rc
            print(f"  done: {post_script.name} finished.", flush=True)
        else:
            print(f"  {step}/{total} uv run ./manage.py migrate", flush=True)
            rc = _run_uv_manage(["migrate"])
            if rc != 0:
                print(f"  failed: migrate exited with {rc}", file=sys.stderr, flush=True)
                return rc
            print("  done: migrate finished.", flush=True)

    rc = run_reset_db_post_manage_commands(cfg)
    if rc != 0:
        return rc
    print("native reset-db: finished successfully.", flush=True)
    return 0


def _run_pg_restore(extra_pg_restore: list[str], *, config: ProjectConfig | None = None) -> int:
    """``pg_restore`` into the app DB (same ``POSTGRES_*`` / ``.env.local`` as ``reset-db``).

    With no ``--file``, ``pg_restore`` reads the archive from **stdin** (pipe or ``< file.dump``).

    With ``--file PATH``, the archive is passed as the **last positional argument** (same as running
    ``pg_restore … path.dump``), not via stdin—feeding stdin plus ``/dev/stdin`` can fail with “magic
    string” on some platforms even when the dump is valid.
    """
    if not shutil.which("pg_restore"):
        print(
            "native pg-restore: `pg_restore` not found on PATH (install PostgreSQL client tools).",
            file=sys.stderr,
        )
        return 127

    extras = list(extra_pg_restore)
    archive_path: Path | None = None
    if "--file" in extras:
        i = extras.index("--file")
        if i + 1 >= len(extras):
            print("native pg-restore: --file requires a path", file=sys.stderr)
            return 1
        path = Path(extras[i + 1]).expanduser()
        if not path.is_file():
            print(f"native pg-restore: not a file: {path}", file=sys.stderr)
            return 1
        archive_path = path
        extras = extras[:i] + extras[i + 2 :]

    cfg = config if config is not None else _config()
    env_file = cfg.env_local_path
    if env_file.is_file():
        print(
            f"native pg-restore: loaded {env_file.name} (same as Django settings)",
            flush=True,
            file=sys.stderr,
        )
    dbname, env = _pg_env_for_cli(cfg)
    flags = _pg_conn_flags(env)
    print(f"native pg-restore: target {_pg_target_line(dbname, env)}", flush=True, file=sys.stderr)
    if archive_path is None:
        print(
            "native pg-restore: reading custom-format archive from stdin (pipe or `< file.dump`)",
            flush=True,
            file=sys.stderr,
        )

    argv = ["pg_restore", *flags, "-d", dbname, *extras]
    if archive_path is not None:
        argv.append(str(archive_path))
        stdin = subprocess.DEVNULL
    else:
        stdin = sys.stdin.buffer if hasattr(sys.stdin, "buffer") else sys.stdin

    print(f"$ {format_shell_command(argv)}", file=sys.stderr, flush=True)
    return run_cmd(argv, env=env, stdin=stdin, check=False, print_cmd=False).returncode


def _run_uv_manage(args: list[str], *, extra_env: dict[str, str] | None = None) -> int:
    cfg = _config()
    merge_local = _django_manage_native_env(cfg)
    env = os.environ.copy()
    # Outer `uv run native …` sets VIRTUAL_ENV to the workspace `.venv`. Nested `uv run` in
    # django_backend targets that project’s env; inherited VIRTUAL_ENV mismatches and uv warns.
    env.pop("VIRTUAL_ENV", None)
    env.update(merge_local)
    if extra_env:
        env.update(extra_env)
    # Run django-rq jobs in-process (no rqworker/rqscheduler) unless the user set RQ_SYNCHRONOUS explicitly.
    if not (env.get("RQ_SYNCHRONOUS") or "").strip():
        env["RQ_SYNCHRONOUS"] = "1"
    path_prepend = f"{Path.home()}/.local/bin:/opt/homebrew/bin:{env.get('PATH', '')}"
    env["PATH"] = path_prepend
    cmd = ["uv", "run", "./manage.py", *args]
    return run_cmd(cmd, cwd=cfg.backend_dir, env=env, check=False).returncode


def _nvm_use_shell_prefix(cwd: Path, node_version: str | None) -> str | None:
    """Return a bash prefix for ``nvm use``, or ``None`` when nvm is unavailable."""
    nvm = Path.home() / ".nvm" / "nvm.sh"
    if not nvm.is_file():
        return None
    nvm_src = f"source {shlex.quote(str(nvm))}"
    if (cwd / ".nvmrc").is_file():
        return f"{nvm_src} && nvm use"
    if node_version and node_version.strip():
        return f"{nvm_src} && nvm use {shlex.quote(node_version.strip())}"
    return None


def _use_nvm_in_cwd(cwd: Path, node_version: str | None = None) -> bool:
    """Whether to wrap npm/yarn/pnpm in nvm (``.nvmrc`` or configured ``node_version``)."""
    return _nvm_use_shell_prefix(cwd, node_version) is not None


def _pkg_install_cmd(package_manager: str) -> list[str]:
    return package_install_cmd(package_manager)


def _pkg_run_cmd(package_manager: str, script: str) -> list[str]:
    return package_run_cmd(package_manager, script)


def _run_pkg_install(cwd: Path, package_manager: str, *, node_version: str | None = None) -> int:
    install_cmd = " ".join(shlex.quote(part) for part in _pkg_install_cmd(package_manager))
    nvm_prefix = _nvm_use_shell_prefix(cwd, node_version)
    if nvm_prefix:
        return run_cmd(
            ["bash", "-lc", f"{nvm_prefix} && {install_cmd}"],
            cwd=cwd,
            check=False,
        ).returncode
    return run_cmd(_pkg_install_cmd(package_manager), cwd=cwd, check=False).returncode


def _run_pkg_script(
    script: str,
    cwd: Path,
    package_manager: str,
    *,
    extra_env: dict[str, str] | None = None,
    node_version: str | None = None,
) -> int:
    run_cmd_parts = _pkg_run_cmd(package_manager, script)
    run_cmd_str = " ".join(shlex.quote(part) for part in run_cmd_parts)
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    nvm_prefix = _nvm_use_shell_prefix(cwd, node_version)
    if nvm_prefix:
        return run_cmd(
            ["bash", "-lc", f"{nvm_prefix} && {run_cmd_str}"],
            cwd=cwd,
            env=env,
            check=False,
        ).returncode
    return run_cmd(run_cmd_parts, cwd=cwd, env=env, check=False).returncode


def _run_frontend_dev() -> int:
    from catalpa_tooling.config import resolve_frontend_package_manager

    cfg = _config()
    frontend_cfg = cfg.native.frontend
    frontend_dir = cfg.frontend_dir
    package_manager = resolve_frontend_package_manager(
        frontend_dir, configured=frontend_cfg.package_manager
    )
    node_version = frontend_cfg.node_version
    if frontend_cfg.install:
        rc = _run_pkg_install(frontend_dir, package_manager, node_version=node_version)
        if rc != 0:
            return rc
    return _run_pkg_script(
        frontend_cfg.script,
        frontend_dir,
        package_manager,
        extra_env=frontend_cfg.env or None,
        node_version=node_version,
    )


def _cmd_fetch_db(*, output: Path | None, dk_env: str, only: str | None = None) -> None:
    warn_deprecated("native fetch db", "dk fetch db")
    cfg = _config()
    overrides: dict[str, Path] = {}
    if output is not None:
        overrides["app"] = output.resolve()
    try:
        run_fetch_all_dbs(
            cfg,
            dk_env=dk_env,
            only=only,
            output_overrides=overrides or None,
        )
    except SystemExit:
        raise


def _cmd_fetch_media(
    *,
    host: str | None,
    dest: Path | None,
    partial: bool,
    legacy_path: bool,
    legacy_remote: str | None,
    dk_env: str | None,
    compose_project: str | None,
) -> None:
    cfg = _config()
    env_name = dk_env if dk_env is not None else cfg.default_fetch_dk_env
    try:
        run_fetch_media(
            cfg,
            dk_env=env_name,
            host=host,
            dest=dest,
            partial=partial,
            legacy_path=legacy_path,
            legacy_remote=legacy_remote,
            compose_project=compose_project,
        )
    except FileNotFoundError as exc:
        print(f"native fetch media: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except ValueError as exc:
        print(f"native fetch media: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _native_main() -> None:
    cfg = _config()
    parser, native_extension_names = build_native_parser(cfg)
    activate(parser)
    args, unknown = parser.parse_known_args()
    # argparse.REMAINDER does not swallow ``--opts`` on ``pg-restore`` once ``--file`` exists;
    # ``parse_known_args`` keeps them so we can forward them to ``pg_restore``.
    allow_unknown = (
        args.command == "pg-restore"
        or (args.command == "reset-db" and getattr(args, "from_dump", None))
        or args.command in native_extension_names
    )
    if unknown and not allow_unknown:
        parser.error(
            "unrecognized arguments: " + " ".join(shlex.quote(u) for u in unknown)
        )
    if args.command == "fetch":
        if args.resource == "db":
            _cmd_fetch_db(
                output=args.output,
                dk_env=args.env if args.env is not None else cfg.default_fetch_dk_env,
            )
            return
        if args.resource == "media":
            _cmd_fetch_media(
                host=args.host,
                dest=args.dest,
                partial=args.partial,
                legacy_path=bool(args.legacy_path),
                legacy_remote=args.remote,
                dk_env=args.env,
                compose_project=args.compose_project,
            )
            return
        return

    handler = args.handler

    if handler == "runserver":
        from catalpa_tooling.config import native_runserver_bind

        extra = [a for a in getattr(args, "django_args", []) if a]
        if not extra:
            bind = native_runserver_bind(cfg.native.django)
            if bind:
                extra = [bind]
        sys.exit(_run_uv_manage(["runserver", *extra]))
    if handler == "manage":
        extra = [a for a in getattr(args, "manage_args", []) if a]
        if not extra:
            print("native manage: pass at least one management command (e.g. migrate).", file=sys.stderr)
            sys.exit(2)
        sys.exit(_run_uv_manage(extra))
    if handler == "reset-db":
        fd = getattr(args, "from_dump", None)
        extras = list(unknown) if fd else []
        sys.exit(
            _run_reset_db_drop_create_migrate_seed(
                from_dump=Path(fd) if fd else None,
                pg_restore_extras=extras,
                explicit_dump=bool(fd),
            )
        )
    if handler == "pg-restore":
        extra = [a for a in getattr(args, "pg_restore_args", []) if a]
        extra.extend(unknown)
        if getattr(args, "archive_file", None):
            extra = ["--file", str(args.archive_file), *extra]
        extra = _pg_restore_extras_skip_remote_roles(extra)
        sys.exit(_run_pg_restore(extra, config=_config()))
    if handler in ("frontend", "vite"):
        sys.exit(_run_frontend_dev())
    if handler == "start":
        sys.exit(run_native_start(cfg))
    if handler == "native-script":
        cfg = _config()
        script_path = getattr(args, "native_script_path", None)
        if script_path is None:
            print("native: internal error (missing native_script_path)", file=sys.stderr)
            sys.exit(2)
        if script_path.name.startswith("dev-"):
            warn_deprecated(
                f"scripts/{script_path.name}",
                f"scripts/native-{script_path.name[len('dev-'):]}",
            )
        elif script_path.name.startswith("local-"):
            warn_deprecated(
                f"scripts/{script_path.name}",
                f"scripts/native-{script_path.name[len('local-'):]}",
            )
        extra = [a for a in getattr(args, "script_args", []) if a]
        extra.extend(unknown)
        sys.exit(run_bash_script(cfg, script_path, extra, label=f"native {args.command}"))

    sys.exit(1)


def main() -> None:
    entry = Path(sys.argv[0]).name
    if entry == "dev":
        warn_deprecated("dev", "native")
    elif entry == "local":
        warn_deprecated("local", "native")
    run_cli(_native_main, label="native")
