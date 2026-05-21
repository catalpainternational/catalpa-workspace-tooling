"""argparse entrypoint for dev: local processes without Docker (Django, Vite, …)."""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv

from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.fetch_media_config import (
    DEFAULT_MEDIA_DK_ENV,
    LEGACY_REMOTE_MEDIA_PATH,
    build_fetch_media_env,
)
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.run_cmd import format_shell_command, run as run_cmd
from catalpa_tooling.script_discovery import (
    discover_dev_commands,
    reset_db_post_script,
)
from catalpa_tooling.script_runner import run_bash_script

DEFAULT_FETCH_DK_ENV = DEFAULT_MEDIA_DK_ENV


def _config() -> ProjectConfig:
    return ProjectConfig.from_cwd()


def _load_env_local(cfg: ProjectConfig) -> None:
    """Load repo-root ``.env.local`` into ``os.environ`` (from ``tooling.yaml`` ``paths.env_local``)."""
    path = cfg.env_local_path
    if path.is_file():
        load_dotenv(path)


def _django_manage_dev_env(cfg: ProjectConfig) -> dict[str, str]:
    """Env merged into every ``uv run ./manage.py …`` invoked by this CLI when keys are unset.

    Loads ``.env.local`` first (when present) so local overrides apply; then sets
    ``DJANGO_DEBUG`` and ``EMAIL_BACKEND_FOLDER`` only if missing — same effect as a
    minimal ``.env.local`` without requiring that file in every worktree.
    """
    _load_env_local(cfg)
    extra: dict[str, str] = {}
    if not (os.environ.get("DJANGO_DEBUG") or "").strip():
        extra["DJANGO_DEBUG"] = "1"
    if not (os.environ.get("EMAIL_BACKEND_FOLDER") or "").strip():
        extra["EMAIL_BACKEND_FOLDER"] = str(cfg.email_backend_dir.resolve())
    return extra


def _pg_env_for_cli() -> tuple[str, dict[str, str]]:
    """Database name from POSTGRES_DB (default ``django``) and env for libpq CLI tools."""
    env = os.environ.copy()
    dbname = (os.environ.get("POSTGRES_DB") or "django").strip() or "django"
    if host := (os.environ.get("POSTGRES_HOST") or "").strip():
        env["PGHOST"] = host
    if port := (os.environ.get("POSTGRES_PORT") or "").strip():
        env["PGPORT"] = port
    if user := (os.environ.get("POSTGRES_USER") or "").strip():
        env["PGUSER"] = user
    if (pw := os.environ.get("POSTGRES_PASSWORD")) is not None:
        env["PGPASSWORD"] = pw
    return dbname, env


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
) -> int:
    """dropdb → createdb → PostGIS → migrate (or ``scripts/dev-reset-db-post.sh``).

    With ``from_dump``, runs ``dropdb`` / ``createdb`` then ``pg_restore`` instead of migrate/seed
    (dump should match how this project dumps, e.g. ``pg_dump -Fc``).

    When ``scripts/dev-reset-db-post.sh`` exists, it runs instead of the built-in ``migrate`` step.
    """
    tools = ["dropdb", "createdb"]
    if from_dump is not None:
        tools.append("pg_restore")
    else:
        tools.append("psql")

    for tool in tools:
        if not shutil.which(tool):
            print(
                f"dev reset-db: `{tool}` not found on PATH (install PostgreSQL client tools).",
                file=sys.stderr,
            )
            return 127

    cfg = _config()
    post_script = reset_db_post_script(cfg.scripts_dir)
    _load_env_local(cfg)
    env_file = cfg.env_local_path
    if env_file.is_file():
        print(f"dev reset-db: loaded {env_file.name} (same as Django settings)", flush=True)

    dump_path: Path | None = None
    if from_dump is not None:
        dump_path = from_dump.expanduser().resolve()
        if not dump_path.is_file():
            print(f"dev reset-db: not a file: {dump_path}", file=sys.stderr)
            return 1

    dbname, env = _pg_env_for_cli()
    flags = _pg_conn_flags(env)

    print("dev reset-db: starting (PostgreSQL client tools)", flush=True)
    print(f"  target: {_pg_target_line(dbname, env)}", flush=True)

    total = 3 if from_dump else 4
    print(
        f"  1/{total} dropdb --if-exists: remove existing database if present "
        f"(this deletes all data in {dbname!r})",
        flush=True,
    )
    rc = run_cmd(["dropdb", "--if-exists", *flags, dbname], env=env, check=False).returncode
    if rc != 0:
        print(f"  failed: dropdb exited with {rc}", file=sys.stderr, flush=True)
        return rc
    print("  done: dropdb finished (database removed or did not exist).", flush=True)

    print(f"  2/{total} createdb: create empty database {dbname!r}", flush=True)
    rc = run_cmd(["createdb", *flags, dbname], env=env, check=False).returncode
    if rc != 0:
        print(f"  failed: createdb exited with {rc}", file=sys.stderr, flush=True)
        return rc
    print("  done: createdb finished (new empty database).", flush=True)

    if dump_path is not None:
        merged = _pg_restore_extras_skip_remote_roles(list(pg_restore_extras or ()))
        extras = ["--file", str(dump_path), *merged]
        print(f"  3/{total} pg_restore: load dump {dump_path}", flush=True)
        rc = _run_pg_restore(extras)
        if rc != 0:
            print(f"  failed: pg_restore exited with {rc}", file=sys.stderr, flush=True)
            return rc
        print("  done: pg_restore finished.", flush=True)
        print("dev reset-db: finished successfully.", flush=True)
        return 0

    print(f"  3/{total} psql: CREATE EXTENSION postgis", flush=True)
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

    if post_script is not None:
        print(f"  4/{total} bash {post_script.name} (project hook)", flush=True)
        rc = run_bash_script(cfg, post_script, [], label="dev reset-db")
        if rc != 0:
            print(f"  failed: {post_script.name} exited with {rc}", file=sys.stderr, flush=True)
            return rc
        print(f"  done: {post_script.name} finished.", flush=True)
    else:
        print(f"  4/{total} uv run ./manage.py migrate", flush=True)
        rc = _run_uv_manage(["migrate"])
        if rc != 0:
            print(f"  failed: migrate exited with {rc}", file=sys.stderr, flush=True)
            return rc
        print("  done: migrate finished.", flush=True)
    print("dev reset-db: finished successfully.", flush=True)
    return 0


def _run_pg_restore(extra_pg_restore: list[str]) -> int:
    """``pg_restore`` into the app DB (same ``POSTGRES_*`` / ``.env.local`` as ``reset-db``).

    With no ``--file``, ``pg_restore`` reads the archive from **stdin** (pipe or ``< file.dump``).

    With ``--file PATH``, the archive is passed as the **last positional argument** (same as running
    ``pg_restore … path.dump``), not via stdin—feeding stdin plus ``/dev/stdin`` can fail with “magic
    string” on some platforms even when the dump is valid.
    """
    if not shutil.which("pg_restore"):
        print(
            "dev pg-restore: `pg_restore` not found on PATH (install PostgreSQL client tools).",
            file=sys.stderr,
        )
        return 127

    extras = list(extra_pg_restore)
    archive_path: Path | None = None
    if "--file" in extras:
        i = extras.index("--file")
        if i + 1 >= len(extras):
            print("dev pg-restore: --file requires a path", file=sys.stderr)
            return 1
        path = Path(extras[i + 1]).expanduser()
        if not path.is_file():
            print(f"dev pg-restore: not a file: {path}", file=sys.stderr)
            return 1
        archive_path = path
        extras = extras[:i] + extras[i + 2 :]

    cfg = _config()
    _load_env_local(cfg)
    env_file = cfg.env_local_path
    if env_file.is_file():
        print(
            f"dev pg-restore: loaded {env_file.name} (same as Django settings)",
            flush=True,
            file=sys.stderr,
        )
    dbname, env = _pg_env_for_cli()
    flags = _pg_conn_flags(env)
    print(f"dev pg-restore: target {_pg_target_line(dbname, env)}", flush=True, file=sys.stderr)
    if archive_path is None:
        print(
            "dev pg-restore: reading custom-format archive from stdin (pipe or `< file.dump`)",
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
    merge_dev = _django_manage_dev_env(cfg)
    env = os.environ.copy()
    # Outer `uv run dev …` sets VIRTUAL_ENV to the workspace `.venv`. Nested `uv run` in
    # django_backend targets that project’s env; inherited VIRTUAL_ENV mismatches and uv warns.
    env.pop("VIRTUAL_ENV", None)
    env.update(merge_dev)
    if extra_env:
        env.update(extra_env)
    # Run django-rq jobs in-process (no rqworker/rqscheduler) unless the user set RQ_SYNCHRONOUS explicitly.
    if not (env.get("RQ_SYNCHRONOUS") or "").strip():
        env["RQ_SYNCHRONOUS"] = "1"
    path_prepend = f"{Path.home()}/.local/bin:/opt/homebrew/bin:{env.get('PATH', '')}"
    env["PATH"] = path_prepend
    cmd = ["uv", "run", "./manage.py", *args]
    return run_cmd(cmd, cwd=cfg.backend_dir, env=env, check=False).returncode


def _use_nvm_in_cwd(cwd: Path) -> bool:
    """Use nvm only when ``cwd`` has ``.nvmrc`` (``nvm use`` without it fails)."""
    nvm = Path.home() / ".nvm" / "nvm.sh"
    return nvm.is_file() and (cwd / ".nvmrc").is_file()


def _run_npm_install(cwd: Path) -> int:
    if _use_nvm_in_cwd(cwd):
        nvm = Path.home() / ".nvm" / "nvm.sh"
        return run_cmd(
            ["bash", "-lc", f"source {nvm} && nvm use && npm install"],
            cwd=cwd,
            check=False,
        ).returncode
    return run_cmd(["npm", "install"], cwd=cwd, check=False).returncode


def _cmd_fetch_db(*, output: Path | None, dk_env: str) -> None:
    cfg = _config()
    root = cfg.repo_root
    out_path = output if output is not None else cfg.fetch_db_dump_path
    out_path = out_path.resolve()
    env = os.environ.copy()
    env["FETCH_DK_ENV"] = dk_env
    env["FETCH_DB_OUTPUT"] = str(out_path)
    script = cfg.scripts_dir / "fetch_db.sh"
    print(f"Running {script}", file=sys.stderr)
    subprocess.run(["bash", str(script)], cwd=root, env=env, check=True)


def _cmd_fetch_media(
    *,
    host: str | None,
    remote_path: str,
    dest: Path | None,
    partial: bool,
    legacy_path: bool,
    dk_env: str,
    compose_project: str | None,
) -> None:
    if not shutil.which("rsync"):
        print("rsync is not installed or not on PATH.", file=sys.stderr)
        raise SystemExit(1)

    cfg = _config()
    root = cfg.repo_root
    local_base = (dest if dest is not None else root / "media").resolve()
    try:
        env = build_fetch_media_env(
            cfg,
            legacy_path=legacy_path,
            dk_env=dk_env,
            host=host,
            remote_path=remote_path,
            dest=dest,
            partial=partial,
            compose_project=compose_project,
        )
    except FileNotFoundError as exc:
        print(f"dev fetch media: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except ValueError as exc:
        print(f"dev fetch media: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    script = cfg.scripts_dir / "fetch_media.sh"
    print(f"Running {script}", file=sys.stderr)
    run_cmd(["bash", str(script)], cwd=root, env=env, check=True)
    print(f"Done: {local_base}", file=sys.stderr)


def _run_npm(script: str, cwd: Path) -> int:
    if _use_nvm_in_cwd(cwd):
        nvm = Path.home() / ".nvm" / "nvm.sh"
        return run_cmd(
            ["bash", "-lc", f"source {nvm} && nvm use && npm run {script}"],
            cwd=cwd,
            check=False,
        ).returncode
    return run_cmd(["npm", "run", script], cwd=cwd, check=False).returncode


def _dev_main() -> None:
    parser = argparse.ArgumentParser(
        prog="dev",
        description="Local development without Docker: Django, frontend npm scripts, fetch db/media.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser(
        "fetch",
        help="Fetch DB via `uv run dk <env> bkp_db pgdump`, or media via rsync (SSH).",
    )
    fetch_sub = fetch.add_subparsers(dest="resource", required=True)

    p_db = fetch_sub.add_parser(
        "db",
        help="Download PostgreSQL custom-format dump via `dk … bkp_db pgdump` (requires `uv`; remote `db` up).",
    )
    p_db.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="PATH",
        help="Output file (default: paths.fetch_db_dump from tooling.yaml)",
    )
    p_db.add_argument(
        "--env",
        default=DEFAULT_FETCH_DK_ENV,
        metavar="NAME",
        help=f"dk environment under docker/envs/ (default: {DEFAULT_FETCH_DK_ENV!r})",
    )

    p_media = fetch_sub.add_parser(
        "media",
        help="Sync media via rsync (requires rsync + SSH). Default: django_media volume on docker_host from info.yaml.",
    )
    p_media.add_argument(
        "--env",
        default=DEFAULT_FETCH_DK_ENV,
        metavar="NAME",
        help=(
            f"dk env for docker_host / compose_project_name when using volume mode (default: {DEFAULT_FETCH_DK_ENV!r}). "
            "Ignored with --legacy-path."
        ),
    )
    p_media.add_argument(
        "--host",
        default=None,
        metavar="USER@HOST",
        help="SSH target. Volume mode: override docker_host from info.yaml. Legacy path mode: required if unset.",
    )
    p_media.add_argument(
        "--remote",
        default=LEGACY_REMOTE_MEDIA_PATH,
        metavar="PATH",
        help=f"Remote media directory with --legacy-path only (default: {LEGACY_REMOTE_MEDIA_PATH})",
    )
    p_media.add_argument("--dest", type=Path, metavar="DIR", help="Local directory (default: <repo>/media)")
    p_media.add_argument(
        "--partial",
        action="store_true",
        help="Sync only documents/ and original_images/ (skip renditions and other dirs). Default is full tree.",
    )
    p_media.add_argument(
        "--legacy-path",
        action="store_true",
        help=(
            f"Rsync from a fixed directory on the SSH host ({LEGACY_REMOTE_MEDIA_PATH} by default) "
            "instead of the django_media Docker volume (docker_host from info.yaml)."
        ),
    )
    p_media.add_argument(
        "--compose-project",
        default=None,
        metavar="NAME",
        help="COMPOSE_PROJECT_NAME for volume name <NAME>_django_media (default: compose_project_name from info.yaml).",
    )

    p_run = subparsers.add_parser("runserver", help="Django dev server (uv run manage.py runserver).")
    p_run.add_argument(
        "django_args",
        nargs=argparse.REMAINDER,
        help="Extra args passed to runserver (e.g. 0.0.0.0:8000).",
    )
    p_run.set_defaults(handler="runserver")

    p_manage = subparsers.add_parser("manage", help="Run any Django management command via uv.")
    p_manage.add_argument(
        "manage_args",
        nargs=argparse.REMAINDER,
        help="Arguments to ./manage.py (e.g. migrate, shell_plus).",
    )
    p_manage.set_defaults(handler="manage")

    p_reset = subparsers.add_parser(
        "reset-db",
        help=(
            "dropdb + createdb + PostGIS + migrate (or scripts/dev-reset-db-post.sh when present; local Postgres)."
        ),
    )
    p_reset.add_argument(
        "--from-dump",
        metavar="PATH",
        dest="from_dump",
        help=(
            "After recreate, pg_restore this custom-format archive instead of migrate/post-hook. "
            "Extra arguments are forwarded to pg_restore (e.g. --no-owner --no-acl)."
        ),
    )
    p_reset.set_defaults(handler="reset-db")

    p_pgrestore = subparsers.add_parser(
        "pg-restore",
        help="pg_restore from stdin (custom format); uses POSTGRES_* from .env.local like reset-db.",
    )
    p_pgrestore.add_argument(
        "--file",
        metavar="PATH",
        dest="archive_file",
        help="Read the custom-format archive from PATH instead of stdin.",
    )
    p_pgrestore.add_argument(
        "pg_restore_args",
        nargs=argparse.REMAINDER,
        help="Extra pg_restore args (e.g. --clean). --no-owner/--no-acl are added if missing. Default archive is stdin.",
    )
    p_pgrestore.set_defaults(handler="pg-restore")

    subparsers.add_parser(
        "vite",
        help="npm install then Vue dev server (paths.frontend from tooling.yaml).",
    ).set_defaults(handler="vite")

    cfg = _config()
    dev_extensions = discover_dev_commands(cfg.scripts_dir)
    dev_extension_names: set[str] = set()
    for cmd_name, script_path in dev_extensions.items():
        dev_extension_names.add(cmd_name)
        rel = script_path.relative_to(cfg.repo_root)
        p_ext = subparsers.add_parser(
            cmd_name,
            help=f"Run project script {rel} (scripts/dev-*.sh).",
        )
        p_ext.add_argument(
            "script_args",
            nargs=argparse.REMAINDER,
            help=f"Arguments forwarded to {script_path.name}.",
        )
        p_ext.set_defaults(handler="dev-script", dev_script_path=script_path)

    args, unknown = parser.parse_known_args()
    # argparse.REMAINDER does not swallow ``--opts`` on ``pg-restore`` once ``--file`` exists;
    # ``parse_known_args`` keeps them so we can forward them to ``pg_restore``.
    allow_unknown = (
        args.command == "pg-restore"
        or (args.command == "reset-db" and getattr(args, "from_dump", None))
        or args.command in dev_extension_names
    )
    if unknown and not allow_unknown:
        parser.error(
            "unrecognized arguments: " + " ".join(shlex.quote(u) for u in unknown)
        )
    if args.command == "fetch":
        if args.resource == "db":
            _cmd_fetch_db(output=args.output, dk_env=args.env)
            return
        if args.resource == "media":
            _cmd_fetch_media(
                host=args.host,
                remote_path=args.remote,
                dest=args.dest,
                partial=args.partial,
                legacy_path=bool(args.legacy_path),
                dk_env=args.env,
                compose_project=args.compose_project,
            )
            return
        return

    handler = args.handler

    if handler == "runserver":
        extra = [a for a in getattr(args, "django_args", []) if a]
        sys.exit(_run_uv_manage(["runserver", *extra]))
    if handler == "manage":
        extra = [a for a in getattr(args, "manage_args", []) if a]
        if not extra:
            print("dev manage: pass at least one management command (e.g. migrate).", file=sys.stderr)
            sys.exit(2)
        sys.exit(_run_uv_manage(extra))
    if handler == "reset-db":
        fd = getattr(args, "from_dump", None)
        extras = list(unknown) if fd else []
        sys.exit(
            _run_reset_db_drop_create_migrate_seed(
                from_dump=Path(fd) if fd else None,
                pg_restore_extras=extras,
            )
        )
    if handler == "pg-restore":
        extra = [a for a in getattr(args, "pg_restore_args", []) if a]
        extra.extend(unknown)
        if getattr(args, "archive_file", None):
            extra = ["--file", str(args.archive_file), *extra]
        extra = _pg_restore_extras_skip_remote_roles(extra)
        sys.exit(_run_pg_restore(extra))
    if handler == "vite":
        cfg = _config()
        rc = _run_npm_install(cfg.frontend_dir)
        if rc != 0:
            sys.exit(rc)
        sys.exit(_run_npm("dev", cfg.frontend_dir))
    if handler == "dev-script":
        cfg = _config()
        script_path = getattr(args, "dev_script_path", None)
        if script_path is None:
            print("dev: internal error (missing dev_script_path)", file=sys.stderr)
            sys.exit(2)
        extra = [a for a in getattr(args, "script_args", []) if a]
        extra.extend(unknown)
        sys.exit(run_bash_script(cfg, script_path, extra, label=f"dev {args.command}"))

    sys.exit(1)


def main() -> None:
    run_cli(_dev_main, label="dev")
