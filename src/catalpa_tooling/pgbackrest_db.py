"""pgBackRest helpers for ``dk <env> bkp_db`` (see README_PGBACKREST.md)."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from catalpa_tooling.config import ProjectConfig, default_pgbackrest_restore_temp_prefix
from catalpa_tooling.pgbackrest_volume_config import (
    _docker_env_for_remote,
    _pgdata_has_control_file,
    conflict_error_message,
    describe_pgbackrest_conf_status,
    ensure_db_compose_volumes,
    ensure_external_stack_volumes,
    ensure_pgbackrest_conf_before_restore,
    ensure_postgres_data_volume,
    expected_pgbackrest_repo_settings,
    materialize_configs,
    pgbackrest_managed_conf_materialized,
    pgbackrest_stanza_exists_in_repo,
    postgres_data_volume_name,
    postgres_pg1_path,
    resolve_mode,
    run_pgbackrest_stanza_create,
    stanza_from_env_for_pgbackrest,
)
from catalpa_tooling.cli_confirm import confirm_by_typing_env_name
from catalpa_tooling.post_db_restore import (
    run_post_db_restore_manage_commands,
    run_post_metabase_db_restore_manage_commands,
)
from catalpa_tooling.run_cmd import format_shell_command, run as run_cmd
from catalpa_tooling.run_cmd import run_interruptible

BackupType = Literal["full", "incr", "diff"]

# First bytes of ``pg_dump -Fc`` custom format (must not be preceded by other stdout).
_PG_DUMP_CUSTOM_MAGIC = b"PGDMP"

# Compose stacks (e.g. catalpa-site) set DJANGO_DB / DJANGO_DB_USER on the db service.
# Older tooling/docs used DJANGO_APP_DB*; resolve both inside the container shell.
_DJANGO_APP_SHELL_VARS = (
    'APP_DB="${DJANGO_APP_DB:-${DJANGO_DB:-catalpa_db}}"; '
    'APP_USER="${DJANGO_APP_DB_USER:-${DJANGO_DB_USER:-catalpa}}"; '
)

DbRestoreTarget = Literal["app", "metabase"]


def _db_shell_vars(target: DbRestoreTarget) -> str:
    if target == "metabase":
        return (
            'APP_DB="${METABASE_DB:-metabase_db}"; '
            'APP_USER="${METABASE_DB_USER:-metabase}"; '
        )
    return _DJANGO_APP_SHELL_VARS

# PostgreSQL startup log once crash/archive recovery has finished (English messages).
_PG_RECOVERY_READY_LOG = "database system is ready to accept connections"


def _merged_process_env(env: dict[str, str]) -> dict[str, str]:
    out = os.environ.copy()
    out.update(env)
    return out


def resolve_stanza(env: dict[str, str]) -> str | None:
    """Prefer ``PGBR_STANZA``, else stanza from ``PGBR_S3_WRITE_*`` / ``PGBR_S3_READ_*``."""
    explicit = (env.get("PGBR_STANZA") or "").strip()
    if explicit:
        return explicit
    return stanza_from_env_for_pgbackrest(env)


def validate_pgbackrest_env(env: dict[str, str]) -> str | None:
    """Return error message or None if ok."""
    c = conflict_error_message(env)
    if c:
        return c
    if not resolve_stanza(env):
        return (
            "pgBackRest: set PGBR_STANZA or PGBR_S3_WRITE_STANZA / PGBR_S3_READ_STANZA "
            "(and full PGBR_S3_* repo vars after `bkp_db configure`)."
        )
    return None


def _resolve_pgbr_console_level(env: dict[str, str], *, for_restore: bool = False) -> str:
    v = (env.get("PGBR_LOG_LEVEL_CONSOLE") or "").strip()
    if v:
        return v
    if for_restore:
        return (env.get("PGBR_RESTORE_LOG_LEVEL_CONSOLE") or "").strip()
    return ""


def _log_level_argv(env: dict[str, str], *, for_restore: bool = False) -> list[str]:
    out: list[str] = []
    if (v := _resolve_pgbr_console_level(env, for_restore=for_restore)):
        out.append(f"--log-level-console={v}")
    if (v := (env.get("PGBR_LOG_LEVEL_STDERR") or "").strip()):
        out.append(f"--log-level-stderr={v}")
    return out


def _compose_exec_pgbackrest(
    compose_file: str,
    env: dict[str, str],
    *pgbackrest_rest: str,
) -> int:
    """``docker compose exec -T -u postgres db pgbackrest …`` (db must be running).

    The ``db`` service often runs as root (entrypoint fixes volume permissions); pgBackRest
    must run as ``postgres`` to connect via the local socket (see systemd/pgbackrest-backup.sh).
    """
    err = validate_pgbackrest_env(env)
    if err:
        print(err, file=sys.stderr)
        return 1
    stanza = resolve_stanza(env)
    assert stanza  # validated above
    argv = [
        "docker",
        "compose",
        "-f",
        compose_file,
        "exec",
        "-T",
        "-u",
        "postgres",
        "db",
        "pgbackrest",
        *_log_level_argv(env),
        f"--stanza={stanza}",
        *pgbackrest_rest,
    ]
    r = run_cmd(
        argv,
        env=_merged_process_env(env),
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return r.returncode


def db_service_responds(compose_file: str, env: dict[str, str]) -> bool:
    """True if ``docker compose exec db true`` succeeds."""
    r = run_cmd(
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "exec",
            "-T",
            "db",
            "true",
        ],
        env=_merged_process_env(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        print_cmd=False,
    )
    return r.returncode == 0


def ensure_db_service_running(
    compose_file: str,
    env: dict[str, str],
    *,
    config: ProjectConfig | None = None,
    dk_env_name: str | None = None,
) -> int:
    """Start ``db`` with ``docker compose up -d db --wait`` when it does not respond to exec."""
    if db_service_responds(compose_file, env):
        return 0
    bind_kwargs: dict = {}
    if config is not None and dk_env_name:
        from catalpa_tooling.storage_config import volume_bind_kwargs

        bind_kwargs = volume_bind_kwargs(config, dk_env_name)
    rc = ensure_db_compose_volumes(env, config=config, **bind_kwargs)
    if rc != 0:
        return rc
    print(
        "bkp_db: `db` service is not running; starting it with "
        "`docker compose up -d db --wait` …",
        file=sys.stderr,
    )
    r = run_cmd(
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "up",
            "-d",
            "db",
            "--wait",
        ],
        env=_merged_process_env(env),
        stdin=subprocess.DEVNULL,
        check=False,
        print_cmd=True,
    )
    if r.returncode != 0:
        print("bkp_db: could not start `db` service.", file=sys.stderr)
        return r.returncode
    if not db_service_responds(compose_file, env):
        print("bkp_db: `db` service did not become ready.", file=sys.stderr)
        return 1
    return 0


def run_bkp_db_stanza_create_flow(
    compose_file: str,
    env: dict[str, str],
    *,
    image: str,
    config: ProjectConfig | None = None,
) -> int:
    """Materialize conf if needed, ensure PGDATA, skip when stanza exists, then ``stanza-create``."""
    if not pgbackrest_managed_conf_materialized(env, config=config):
        print(
            "pgBackRest stanza-create: materializing volume config from env …",
            file=sys.stderr,
        )
        rc = materialize_configs(
            env, dry_run=False, postgres_image=image, config=config
        )
        if rc != 0:
            return rc

    if pgbackrest_stanza_exists_in_repo(env, image=image, config=config):
        print(
            "pgBackRest: stanza is healthy in repository (info status ok); skipping stanza-create.",
            file=sys.stderr,
        )
        return 0

    docker_env = _docker_env_for_remote(env)
    vol_data = postgres_data_volume_name(env, config=config)
    pg1 = postgres_pg1_path(env, config=config)
    if not _pgdata_has_control_file(docker_env, image, vol_data, pg1_path=pg1):
        print(
            "pgBackRest stanza-create: initializing PostgreSQL (starting `db`) …",
            file=sys.stderr,
        )
        rc = ensure_db_service_running(compose_file, env, config=config)
        if rc != 0:
            return rc
        if not _pgdata_has_control_file(docker_env, image, vol_data, pg1_path=pg1):
            print(
                "pgBackRest stanza-create: PGDATA still missing after starting `db`.",
                file=sys.stderr,
            )
            return 1

    return run_pgbackrest_stanza_create(env, image=image, config=config)


def run_bkp_db_init(
    compose_file: str,
    env: dict[str, str],
    *,
    image: str,
    config: ProjectConfig | None = None,
    dk_env_name: str | None = None,
) -> int:
    """Greenfield backup host: volumes, materialize, start ``db``, idempotent stanza-create."""
    bind_kwargs: dict = {}
    if config is not None and dk_env_name:
        from catalpa_tooling.storage_config import volume_bind_kwargs

        bind_kwargs = volume_bind_kwargs(config, dk_env_name)
    rc = ensure_external_stack_volumes(env, config=config, **bind_kwargs)
    if rc != 0:
        return rc
    rc = materialize_configs(env, dry_run=False, postgres_image=image, config=config)
    if rc != 0:
        return rc
    rc = ensure_db_service_running(compose_file, env, config=config, dk_env_name=dk_env_name)
    if rc != 0:
        return rc
    return run_bkp_db_stanza_create_flow(
        compose_file, env, image=image, config=config
    )


def _pg_restore_owner_acl_extras(extras: Sequence[str]) -> list[str]:
    """Prepend ``--no-owner`` / ``--no-acl`` when missing (dumps from other hosts)."""
    xs = list(extras)
    if "--no-owner" not in xs:
        xs.insert(0, "--no-owner")
    if "--no-acl" not in xs and "--no-privileges" not in xs:
        xs.insert(0, "--no-acl")
    return xs


def pg_restore_compose_extras(extras: Sequence[str] | None = None) -> list[str]:
    """``pg_restore`` flags for compose restore (``dk transfer``, ``bkp_db pgrestore``)."""
    return _pg_restore_owner_acl_extras(list(extras or ()))


def compose_pg_restore_extras_for_config(
    config: ProjectConfig,
    extras: Sequence[str] | None = None,
    *,
    default_archive: Path | None = None,
) -> list[str]:
    """Merge ``native.reset_db.pg_restore_args`` with CLI extras for compose ``pg_restore``."""
    merged = [*config.native.reset_db.pg_restore_args, *(extras or ())]
    return pg_restore_compose_extras(
        pg_restore_extras_with_default_archive(merged, default_archive),
    )


def _pg_restore_has_role(extras: Sequence[str]) -> bool:
    for i, arg in enumerate(extras):
        if arg == "--role":
            return True
        if arg.startswith("--role="):
            return True
    return False


def _pg_restore_explicit_role(extras: Sequence[str]) -> str | None:
    """Return an explicit ``--role`` value from ``pg_restore`` extras, if any."""
    for i, arg in enumerate(extras):
        if arg == "--role" and i + 1 < len(extras):
            return extras[i + 1]
        if arg.startswith("--role="):
            return arg.partition("=")[2]
    return None


def _pg_restore_compose_role_suffix(
    extras: Sequence[str],
    *,
    postgis: bool = False,
    restore_as_super: bool = False,
) -> str:
    """Shell suffix for compose ``pg_restore`` so ``--no-owner`` objects get a restore role.

    When ``restore_as_super`` is true, use ``APP_USER`` (with temporary superuser promotion).
    When ``postgis`` is true and ``restore_as_super`` is false, use ``postgres`` so
    ``COMMENT ON EXTENSION`` and extension catalog reload succeed.
    Otherwise default to ``APP_USER``.
    """
    if _pg_restore_has_role(extras):
        return ""
    if restore_as_super:
        return ' --role "$APP_USER"'
    if postgis:
        return " --role postgres"
    return ' --role "$APP_USER"'


def _pg_restore_promote_app_superuser(
    extras: Sequence[str],
    *,
    target: DbRestoreTarget,
    restore_as_super: bool = False,
) -> bool:
    """Whether to temporarily grant superuser to ``APP_USER`` around compose restore.

    Enabled when ``native.reset_db.restore_as_super`` is true (default: false).
    Restoring as the app user keeps dbsamizdat-owned matviews under ``APP_USER``, while
    short-lived superuser allows extension DDL/comments from production dumps (PostGIS,
    pgcrypto, etc.) without ``--role postgres`` (which leaves app objects superuser-owned).
    Skipped when ``--role postgres`` is set explicitly in ``pg_restore_args``.
    """
    if not restore_as_super:
        return False
    if target != "app":
        return False
    explicit = _pg_restore_explicit_role(extras)
    return explicit is None or explicit != "postgres"


def _pg_restore_compose_inner_script(
    target: DbRestoreTarget,
    extras: Sequence[str],
    *,
    container_path: str,
    promote_app_superuser: bool,
    postgis: bool = False,
    restore_as_super: bool = False,
) -> str:
    """Shell script run in the ``db`` container to execute ``pg_restore``."""
    shell_vars = _db_shell_vars(target)
    pg_restore_cmd = 'pg_restore -h 127.0.0.1 -p 5432 -U postgres -d "$APP_DB"'
    if extras:
        pg_restore_cmd += " " + " ".join(shlex.quote(a) for a in extras)
    pg_restore_cmd += _pg_restore_compose_role_suffix(
        extras,
        postgis=postgis,
        restore_as_super=restore_as_super,
    )
    pg_restore_cmd += " " + shlex.quote(container_path)

    if not promote_app_superuser:
        return (
            shell_vars
            + 'export PGPASSWORD="$POSTGRES_PASSWORD"; '
            + pg_restore_cmd
        )

    return f"""set -eu
{shell_vars}
export PGPASSWORD="$POSTGRES_PASSWORD"
_promoted=0
demote_app_user() {{
  if [ "$_promoted" -eq 1 ]; then
    psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -v ON_ERROR_STOP=0 \\
      -c "ALTER ROLE \\"$APP_USER\\" NOSUPERUSER;" >/dev/null 2>&1 || true
  fi
}}
trap demote_app_user EXIT
_super=$(psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -Atqc \\
  "SELECT rolsuper FROM pg_roles WHERE rolname = '$APP_USER'")
if [ "$_super" != "t" ]; then
  psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -v ON_ERROR_STOP=1 \\
    -c "ALTER ROLE \\"$APP_USER\\" SUPERUSER;"
  _promoted=1
fi
{pg_restore_cmd}
"""


def pg_restore_extras_with_default_archive(
    extras: Sequence[str] | None,
    default_archive: Path | None,
) -> list[str]:
    """Return ``pg_restore`` CLI extras, using ``default_archive`` when stdin is a TTY and no ``--file``."""
    xs = list(extras or ())
    if "--file" in xs:
        return xs
    if not sys.stdin.isatty():
        return xs
    if default_archive is None:
        return xs
    path = default_archive.expanduser()
    if not path.is_file() or path.stat().st_size == 0:
        print(
            f"bkp_db pgrestore: no dump at {path} — fetch one first "
            "(e.g. `uv run native fetch db`) or pass `--file PATH`.",
            file=sys.stderr,
        )
        return xs
    resolved = path.resolve()
    print(f"bkp_db pgrestore: using default archive {resolved}", file=sys.stderr)
    return ["--file", str(resolved), *xs]


def run_info(compose_file: str, env: dict[str, str]) -> int:
    return _compose_exec_pgbackrest(compose_file, env, "info")


def run_check_online(compose_file: str, env: dict[str, str]) -> int:
    return _compose_exec_pgbackrest(compose_file, env, "check")


def run_configure_verify_online_check(compose_file: str, env: dict[str, str]) -> int:
    """After ``run_pgbackrest_verify`` preflight: ``pgbackrest check`` inside the ``db`` service."""
    if resolve_mode(env) == "none":
        return 0
    err = validate_pgbackrest_env(env)
    if err:
        print(err, file=sys.stderr)
        return 1
    if not db_service_responds(compose_file, env):
        print(
            "pgBackRest verify: `db` service is not running; skipped online `pgbackrest check`. "
            "Start the stack, then run `dk <env> bkp_db check` or re-run `bkp_db configure verify`.",
            file=sys.stderr,
        )
        return 0
    return run_check_online(compose_file, env)


def run_version(compose_file: str, env: dict[str, str]) -> int:
    return _compose_exec_pgbackrest(compose_file, env, "version")


def _pg_dump_inner_script(extra_pg_dump_args: Sequence[str] | None) -> str:
    extras = tuple(extra_pg_dump_args or ())
    inner = (
        _DJANGO_APP_SHELL_VARS
        + 'export PGPASSWORD="$POSTGRES_PASSWORD"; '
        + 'exec pg_dump -h 127.0.0.1 -p 5432 -U postgres -d "$APP_DB" -Fc'
    )
    if extras:
        inner = inner + " " + " ".join(shlex.quote(a) for a in extras)
    return inner


def run_pg_dump(
    compose_file: str,
    env: dict[str, str],
    extra_pg_dump_args: Sequence[str] | None = None,
) -> int:
    """Run ``pg_dump`` in the ``db`` container against the app DB (``DJANGO_DB`` / ``DJANGO_APP_DB``); stream to stdout.

    Uses the superuser inside the container (``postgres`` / ``POSTGRES_PASSWORD``). Optional
    ``extra_pg_dump_args`` are appended (shell-quoted). Command echo is suppressed so stdout is
    suitable for ``> file`` or piping.
    """
    inner = _pg_dump_inner_script(extra_pg_dump_args)
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
        env=_merged_process_env(env),
        stdin=subprocess.DEVNULL,
        check=False,
        print_cmd=False,
    )
    return r.returncode


def run_pg_dump_to_file(
    compose_file: str,
    env: dict[str, str],
    dest: Path,
    extra_pg_dump_args: Sequence[str] | None = None,
) -> int:
    """Same as ``run_pg_dump`` but writes the ``-Fc`` archive to ``dest`` (binary-safe).

    Writes to a ``.tmp`` sibling first, then replaces ``dest`` on success.
    """
    inner = _pg_dump_inner_script(extra_pg_dump_args)
    cmd = [
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
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    merged = _merged_process_env(env)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with open(tmp, "wb") as out_f:
            r = run_cmd(
                cmd,
                env=merged,
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=subprocess.PIPE,
                check=False,
                print_cmd=True,
            )
        if r.returncode != 0:
            err = r.stderr or b""
            if err:
                sys.stderr.buffer.write(err)
            try:
                tmp.unlink()
            except OSError:
                pass
            return r.returncode
        tmp.replace(dest)
        return 0
    except OSError as e:
        print(f"pg_dump to file: {e}", file=sys.stderr)
        try:
            tmp.unlink()
        except OSError:
            pass
        return 1


def _drop_create_app_database_psql_block(*, postgis: bool) -> str:
    """``psql`` heredoc run after ``createdb`` (grants; optional PostGIS prep for dump restore).

    When ``postgis`` is true, create PostGIS as superuser and grant catalog tables to the
    app user before ``pg_restore``. Compose restore uses ``--role postgres`` when
    ``postgis`` is true and ``restore_as_super`` is false; use ``restore_as_super: true``
    for ``--role APP_USER`` with temporary superuser promotion instead.
    """
    lines = [
        "GRANT ALL PRIVILEGES ON DATABASE ${APP_DB} TO ${APP_USER};",
    ]
    if postgis:
        lines.extend(
            [
                "CREATE EXTENSION IF NOT EXISTS postgis;",
                "GRANT ALL ON ALL TABLES IN SCHEMA public TO ${APP_USER};",
            ]
        )
    lines.extend(
        [
            "GRANT ALL ON SCHEMA public TO ${APP_USER};",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO ${APP_USER};",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO ${APP_USER};",
        ]
    )
    return "\n".join(lines)


def run_drop_create_app_database(
    compose_file: str,
    env: dict[str, str],
    *,
    postgis: bool = False,
    target: DbRestoreTarget = "app",
) -> int:
    """Replace the Django app database with an empty one (grants match project init scripts).

    Runs ``dropdb --force`` (PostgreSQL 13+) so existing connections are terminated, then
    ``createdb -O "$APP_USER"``. When ``postgis`` is true (``tooling.yaml`` ``native.reset_db.postgis``),
    PostGIS is pre-created and its catalog tables are granted to the app user before
    ``pg_restore``. Used by ``dk transfer`` and ``bkp_db pgrestore``.
    """
    merged = _merged_process_env(env)
    psql_body = _drop_create_app_database_psql_block(postgis=postgis)
    shell_vars = _db_shell_vars(target)
    script = f"""set -eu
{shell_vars}
export PGPASSWORD="$POSTGRES_PASSWORD"
dropdb -h 127.0.0.1 -p 5432 -U postgres --if-exists --force "$APP_DB"
createdb -h 127.0.0.1 -p 5432 -U postgres -O "$APP_USER" "$APP_DB"
psql -h 127.0.0.1 -p 5432 -U postgres -d "$APP_DB" -v ON_ERROR_STOP=1 <<EOF
{psql_body}
EOF
"""
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
            script,
        ],
        env=merged,
        stdin=subprocess.DEVNULL,
        check=False,
        print_cmd=False,
    )
    return r.returncode


def run_drop_create_metabase_database(
    compose_file: str,
    env: dict[str, str],
) -> int:
    """Replace the Metabase application database with an empty one."""
    return run_drop_create_app_database(
        compose_file,
        env,
        postgis=False,
        target="metabase",
    )


def run_pg_restore(
    compose_file: str,
    env: dict[str, str],
    extra_pg_restore_args: Sequence[str] | None = None,
    *,
    config: ProjectConfig | None = None,
    target: DbRestoreTarget = "app",
) -> int:
    """Run ``pg_restore`` in the ``db`` container against the app DB (``DJANGO_DB`` / ``DJANGO_APP_DB``).

    Resolves a host path to a ``pg_dump -Fc`` archive (``--file`` or spooled stdin), copies it
    into the container with ``docker compose cp``, then runs ``pg_restore`` on that path.
    Streaming the archive through ``docker compose exec -i`` can corrupt binary data and make
    ``pg_restore`` fail magic/header checks.
    """
    extras = list(extra_pg_restore_args or ())
    temp_spool: str | None = None
    host_archive: Path | None = None
    try:
        if "--file" in extras:
            i = extras.index("--file")
            if i + 1 >= len(extras):
                print("pg_restore: --file requires a path", file=sys.stderr)
                return 1
            path = Path(extras[i + 1]).expanduser()
            if not path.is_file():
                print(f"pg_restore: not a file: {path}", file=sys.stderr)
                return 1
            extras = extras[:i] + extras[i + 2 :]
            with open(path, "rb") as f:
                magic = f.read(5)
            if magic != _PG_DUMP_CUSTOM_MAGIC:
                print(
                    f"pg_restore: {path} does not look like a pg_dump -Fc archive "
                    f"(expected {_PG_DUMP_CUSTOM_MAGIC!r} at the start).",
                    file=sys.stderr,
                )
                return 1
            host_archive = path
        else:
            head = sys.stdin.buffer.read(5)
            if len(head) < 5 or head != _PG_DUMP_CUSTOM_MAGIC:
                print(
                    "pg_restore: stdin is not a pg_dump -Fc archive "
                    f"(expected {_PG_DUMP_CUSTOM_MAGIC!r} at the start). "
                    "Re-dump with `dk <env> bkp_db pgdump` and redirect only pg_dump to the file, "
                    "or use `bkp_db pgrestore --file /path/to.dump`.",
                    file=sys.stderr,
                )
                if head:
                    print(f"  First bytes from stdin: {head!r}", file=sys.stderr)
                return 1
            tf = tempfile.NamedTemporaryFile(prefix="pgrestore-", suffix=".dump", delete=False)
            temp_spool = tf.name
            try:
                tf.write(head)
                shutil.copyfileobj(sys.stdin.buffer, tf)
                tf.flush()
            finally:
                tf.close()
            host_archive = Path(temp_spool)

        merged = _merged_process_env(env)
        prefix = (
            config.ops.pgbackrest.restore_temp_prefix
            if config
            else default_pgbackrest_restore_temp_prefix("pgrestore")
        )
        container_path = f"/tmp/{prefix}{os.urandom(8).hex()}.dump"

        cp = run_cmd(
            [
                "docker",
                "compose",
                "-f",
                compose_file,
                "cp",
                str(host_archive.resolve()),
                f"db:{container_path}",
            ],
            env=merged,
            stdin=subprocess.DEVNULL,
            check=False,
            print_cmd=False,
        )
        if cp.returncode != 0:
            print(
                "pg_restore: `docker compose cp` failed (could not copy the archive into the "
                "`db` service).",
                file=sys.stderr,
            )
            return 1

        # ``docker compose cp`` typically leaves root-owned files mode 600; ``postgres`` cannot read them.
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
                "pg_restore: could not chmod/chown the archive in the `db` container "
                "(expected root + postgres user).",
                file=sys.stderr,
            )
            run_cmd(
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
                    "rm",
                    "-f",
                    container_path,
                ],
                env=merged,
                stdin=subprocess.DEVNULL,
                check=False,
                print_cmd=False,
            )
            return 1

        postgis = config.native.reset_db.postgis if config else False
        restore_as_super = (
            config.native.reset_db.restore_as_super if config else False
        )
        inner = _pg_restore_compose_inner_script(
            target,
            extras,
            container_path=container_path,
            promote_app_superuser=_pg_restore_promote_app_superuser(
                extras,
                target=target,
                restore_as_super=restore_as_super,
            ),
            postgis=postgis,
            restore_as_super=restore_as_super,
        )
        try:
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
                env=merged,
                stdin=subprocess.DEVNULL,
                check=False,
                print_cmd=False,
            )
            return r.returncode
        finally:
            run_cmd(
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
                    "rm",
                    "-f",
                    container_path,
                ],
                env=merged,
                stdin=subprocess.DEVNULL,
                check=False,
                print_cmd=False,
            )
    finally:
        if temp_spool is not None:
            try:
                os.unlink(temp_spool)
            except OSError:
                pass


def run_backup(compose_file: str, env: dict[str, str], backup_type: BackupType) -> int:
    return _compose_exec_pgbackrest(compose_file, env, "backup", f"--type={backup_type}")


def _compose_stop_db(compose_file: str, env: dict[str, str]) -> int:
    """``docker compose stop db`` (idempotent)."""
    r = run_interruptible(
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "stop",
            "db",
        ],
        env=_merged_process_env(env),
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return r.returncode


def _compose_up_db(
    compose_file: str, env: dict[str, str], *, force_recreate: bool = False
) -> int:
    """``docker compose up -d db`` (optionally ``--force-recreate`` after offline restore)."""
    argv = [
        "docker",
        "compose",
        "-f",
        compose_file,
        "up",
        "-d",
    ]
    if force_recreate:
        argv.append("--force-recreate")
    argv.append("db")
    r = run_interruptible(
        argv,
        env=_merged_process_env(env),
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return r.returncode


def _oneoff_db_container_filters(env: dict[str, str]) -> list[str]:
    filters = [
        "label=com.docker.compose.oneoff=True",
        "label=com.docker.compose.service=db",
    ]
    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip()
    if project:
        filters.append(f"label=com.docker.compose.project={project}")
    return filters


def _list_oneoff_db_container_ids(env: dict[str, str]) -> list[str]:
    merged = _merged_process_env(env)
    ps_argv = ["docker", "ps", "-q"]
    for f in _oneoff_db_container_filters(env):
        ps_argv.extend(["--filter", f])
    listed = run_cmd(
        ps_argv,
        env=merged,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    ids = [line for line in (listed.stdout or "").splitlines() if line.strip()]
    if ids:
        return ids
    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip()
    if not project:
        return []
    # Fallback: ``docker compose run db`` names containers ``{project}-db-run-<hash>``.
    listed = run_cmd(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            f"name={project}-db-run-",
        ],
        env=merged,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    return [line for line in (listed.stdout or "").splitlines() if line.strip()]


def _remove_interrupted_compose_run_db(compose_file: str, env: dict[str, str]) -> None:
    """Stop and remove one-off ``db`` containers left when ``compose run`` is interrupted."""
    ids = _list_oneoff_db_container_ids(env)
    if not ids:
        print(
            "pgBackRest restore: no interrupted one-off `db` container found to stop.",
            file=sys.stderr,
        )
        return
    merged = _merged_process_env(env)
    print(
        f"pgBackRest restore: stopping interrupted one-off `db` container(s): "
        f"{', '.join(ids)}…",
        file=sys.stderr,
    )
    run_cmd(
        ["docker", "kill", *ids],
        env=merged,
        stdin=subprocess.DEVNULL,
        check=False,
        print_cmd=False,
    )
    run_cmd(
        ["docker", "rm", "-f", *ids],
        env=merged,
        stdin=subprocess.DEVNULL,
        check=False,
        print_cmd=False,
    )


def _restore_recovery_timeout_sec(env: dict[str, str]) -> int:
    raw = (env.get("PGBR_RESTORE_RECOVERY_TIMEOUT_SEC") or "").strip()
    if not raw:
        return 3600
    try:
        n = int(raw, 10)
    except ValueError:
        return 3600
    return max(30, n)


def _restore_db_logs_silenced(merged_env: dict[str, str]) -> bool:
    """True when ``PGBR_RESTORE_SILENCE_DB_LOGS`` disables streaming ``docker compose logs -f db``."""
    v = (merged_env.get("PGBR_RESTORE_SILENCE_DB_LOGS") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _terminate_log_follower(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def wait_db_logs_for_recovery_ready(
    compose_file: str,
    env: dict[str, str],
    *,
    timeout_sec: int,
    since: str | None = None,
) -> tuple[bool, str]:
    """Follow ``docker compose logs -f db`` until ``_PG_RECOVERY_READY_LOG`` appears or timeout.

    Each log line is copied to stderr unless ``PGBR_RESTORE_SILENCE_DB_LOGS`` is truthy in the
    merged process environment (see ``_restore_db_logs_silenced``).

    When ``since`` is set (RFC3339 or relative, e.g. ``2026-05-20T03:14:00Z``), only log lines
    after that timestamp are considered — avoids matching a previous container start.

    Returns ``(True, "")`` on success, or ``(False, reason)`` on failure/timeout.
    Raises ``KeyboardInterrupt`` when the user presses Ctrl-C (after stopping log follow).
    """
    merged = _merged_process_env(env)
    silence_db_logs = _restore_db_logs_silenced(merged)
    argv = [
        "docker",
        "compose",
        "-f",
        compose_file,
        "logs",
        "-f",
    ]
    if since:
        argv.extend(["--since", since])
    argv.append("db")
    proc = subprocess.Popen(
        argv,
        env=merged,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    ready = threading.Event()

    def _read_loop() -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if not silence_db_logs:
                    print(line, end="", file=sys.stderr, flush=True)
                if _PG_RECOVERY_READY_LOG in line:
                    ready.set()
                    return
        except Exception:
            return

    th = threading.Thread(target=_read_loop, daemon=True)
    th.start()
    old_sigint = signal.getsignal(signal.SIGINT)

    def _sigint_during_logs(signum: int, frame: object | None) -> None:
        _terminate_log_follower(proc)
        th.join(timeout=5)
        signal.signal(signal.SIGINT, old_sigint)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_during_logs)
    try:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            remaining = min(1.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            if ready.wait(timeout=remaining):
                return True, ""
            if proc.poll() is not None:
                return (
                    False,
                    "`docker compose logs -f db` exited before PostgreSQL reported recovery complete.",
                )
        if proc.poll() is not None:
            return (
                False,
                "`docker compose logs -f db` exited before PostgreSQL reported recovery complete.",
            )
        return (
            False,
            f"timed out after {timeout_sec}s waiting for `{_PG_RECOVERY_READY_LOG}` in `db` logs.",
        )
    except KeyboardInterrupt:
        _terminate_log_follower(proc)
        th.join(timeout=5)
        raise
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        _terminate_log_follower(proc)
        th.join(timeout=5)


def build_restore_offline_argv(
    env: dict[str, str],
    *,
    compose_file: str,
    extra_pgbackrest_args: Sequence[str] | None = None,
) -> list[str] | None:
    """Return ``docker compose run … pgbackrest restore`` argv, or ``None`` if env is invalid."""
    err = validate_pgbackrest_env(env)
    if err:
        return None
    stanza = resolve_stanza(env)
    if not stanza:
        return None
    extras = tuple(extra_pgbackrest_args or ())
    extra_shell = " ".join(shlex.quote(a) for a in extras)
    inner = (
        f"pgbackrest {_log_level_argv_shell(env, for_restore=True)}"
        f"--stanza={shlex.quote(stanza)} restore --delta"
    )
    if extra_shell:
        inner = f"{inner} {extra_shell}"
    return [
        "docker",
        "compose",
        "-f",
        compose_file,
        "run",
        "-T",
        "--rm",
        "--no-deps",
        "-u",
        "postgres",
        "--entrypoint",
        "/bin/sh",
        "db",
        "-c",
        inner,
    ]


def plan_restore_offline(
    env: dict[str, str],
    *,
    compose_file: str,
    env_name: str,
    extra_pgbackrest_args: Sequence[str] | None = None,
    config: ProjectConfig | None = None,
    docker_host: str = "",
) -> int:
    """Print the offline pgBackRest restore plan (``dk <env> db restore --dry-run``)."""
    err = validate_pgbackrest_env(env)
    if err:
        print(err, file=sys.stderr)
        return 1
    stanza = resolve_stanza(env)
    if not stanza:
        print("pgBackRest restore: could not determine stanza from env.", file=sys.stderr)
        return 1

    mode = resolve_mode(env)
    expected = expected_pgbackrest_repo_settings(env, config=config)
    pg1 = postgres_pg1_path(env, config=config)
    data_vol = postgres_data_volume_name(env, config=config)
    restore_argv = build_restore_offline_argv(
        env,
        compose_file=compose_file,
        extra_pgbackrest_args=extra_pgbackrest_args,
    )
    if restore_argv is None:
        print("pgBackRest restore: could not build restore command.", file=sys.stderr)
        return 1

    print("dry-run: pgBackRest offline restore plan", file=sys.stderr)
    print(f"  Environment: {env_name}", file=sys.stderr)
    print(f"  Compose file: {compose_file}", file=sys.stderr)
    dh = (docker_host or env.get("DOCKER_HOST") or "").strip()
    print(f"  DOCKER_HOST: {dh or '(default local socket)'}", file=sys.stderr)
    print(f"  pgBackRest mode: {mode}", file=sys.stderr)
    print(f"  Stanza: {stanza}", file=sys.stderr)
    if expected:
        print(f"  S3 bucket: {expected.bucket}", file=sys.stderr)
        print(f"  S3 region: {expected.region or '(default)'}", file=sys.stderr)
        if expected.endpoint:
            print(f"  S3 endpoint: {expected.endpoint}", file=sys.stderr)
        print(f"  repo1-path: {expected.repo_path}", file=sys.stderr)
    print(f"  PGDATA volume: {data_vol}", file=sys.stderr)
    print(f"  pg1-path: {pg1}", file=sys.stderr)
    print(f"  Volume config: {describe_pgbackrest_conf_status(env, config=config)}", file=sys.stderr)
    print(
        "  Steps: ensure volumes → sync pgBackRest config if needed → "
        "stop `db` if running → pgBackRest restore → "
        "`docker compose up -d --force-recreate db` → wait for recovery → post-restore hooks",
        file=sys.stderr,
    )
    print(f"  pgBackRest command: {format_shell_command(restore_argv)}", file=sys.stderr)
    if config is not None:
        run_post_db_restore_manage_commands(
            config,
            compose_file=compose_file,
            env_add=env,
            env_name=env_name,
            dry_run=True,
        )
    return 0


def run_restore_offline(
    env: dict[str, str],
    *,
    compose_file: str,
    env_name: str,
    skip_confirm: bool,
    extra_pgbackrest_args: Sequence[str] | None = None,
    config: ProjectConfig | None = None,
) -> int:
    """Run ``pgbackrest --stanza=… restore --delta`` in a one-off ``db`` container (Compose ``run``).

    Stops the long-running ``db`` service first so PGDATA is not in use, then runs
    ``docker compose run --rm --no-deps`` with the same mounts/env as the ``db`` service.
    On success, starts ``db`` with ``docker compose up -d db`` and waits until container logs
    contain PostgreSQL's ``database system is ready to accept connections`` (recovery complete).
    Override the wait with env ``PGBR_RESTORE_RECOVERY_TIMEOUT_SEC`` (default ``3600``, minimum ``30``).
    While waiting, ``db`` container logs stream to stderr; set ``PGBR_RESTORE_SILENCE_DB_LOGS``
    to ``1`` / ``true`` / ``yes`` / ``on`` to disable that stream.
    ``--delta`` allows a non-empty PGDATA and is typically faster when data already overlaps the backup.
    ``extra_pgbackrest_args`` are appended (e.g. PITR: ``--type=time``, ``--target=…``); each token is shell-quoted.
    Console level: ``PGBR_LOG_LEVEL_CONSOLE``, else ``PGBR_RESTORE_LOG_LEVEL_CONSOLE`` (from ``tooling.yaml`` / ``info.yaml``).
    """
    err = validate_pgbackrest_env(env)
    if err:
        print(err, file=sys.stderr)
        return 1
    stanza = resolve_stanza(env)
    assert stanza

    from catalpa_tooling.storage_config import volume_bind_kwargs

    bind_kwargs = volume_bind_kwargs(config, env_name) if config is not None else {}
    if ensure_postgres_data_volume(env, config=config, **bind_kwargs) != 0:
        return 1

    if (
        ensure_pgbackrest_conf_before_restore(
            env,
            config=config,
            skip_configure_confirm=skip_confirm,
        )
        != 0
    ):
        return 1

    if not skip_confirm:
        print(
            "WARNING: This runs pgBackRest restore into the stack's postgres_data volume. "
            "If `db` is running, it will be stopped first. After pgBackRest finishes, `db` is "
            "started again so PostgreSQL can complete recovery.\n"
            "See README_PGBACKREST.md for recovery procedures.",
            file=sys.stderr,
        )
        print(f"  Environment: {env_name}", file=sys.stderr)
        if not confirm_by_typing_env_name(env_name):
            print("Cancelled.", file=sys.stderr)
            return 1

    if db_service_responds(compose_file, env):
        print(
            "pgBackRest restore: stopping `db` so the data directory is not in use…",
            file=sys.stderr,
        )
        rc = _compose_stop_db(compose_file, env)
        if rc == 130:
            print("pgBackRest restore: cancelled.", file=sys.stderr)
            return rc
        if rc != 0:
            print(
                f"pgBackRest restore: `docker compose -f {compose_file} stop db` failed "
                f"(exit {rc}). Stop `db` manually, then retry.",
                file=sys.stderr,
            )
            return rc
        if db_service_responds(compose_file, env):
            print(
                "pgBackRest restore: `db` still appears to be running after `compose stop db`. "
                "Stop it manually, then retry.",
                file=sys.stderr,
            )
            return 1

    restore_argv = build_restore_offline_argv(
        env,
        compose_file=compose_file,
        extra_pgbackrest_args=extra_pgbackrest_args,
    )
    if restore_argv is None:
        print("pgBackRest restore: could not build restore command.", file=sys.stderr)
        return 1

    r = run_interruptible(
        restore_argv,
        env=_merged_process_env(env),
        stdin=subprocess.DEVNULL,
        on_interrupt=lambda: _remove_interrupted_compose_run_db(compose_file, env),
    )
    if r.returncode == 130:
        print("pgBackRest restore: cancelled.", file=sys.stderr)
        print(
            "The `db` service was stopped for restore and was not restarted. "
            "Start it with `docker compose up -d db` when ready.",
            file=sys.stderr,
        )
        return r.returncode
    if r.returncode != 0:
        print("pgBackRest: restore failed.", file=sys.stderr)
        return r.returncode

    print(
        "pgBackRest restore: pgBackRest finished; recreating `db` and waiting for PostgreSQL "
        "recovery (log line: database system is ready to accept connections)…",
        file=sys.stderr,
    )
    logs_since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rc_up = _compose_up_db(compose_file, env, force_recreate=True)
    if rc_up == 130:
        print("pgBackRest restore: cancelled during `db` startup.", file=sys.stderr)
        print(
            "The data directory was restored; start `db` with `docker compose up -d db` when ready.",
            file=sys.stderr,
        )
        return rc_up
    if rc_up != 0:
        print(
            f"pgBackRest restore: `docker compose up -d --force-recreate db` failed (exit {rc_up}). "
            "The data directory was restored; start `db` manually when ready.",
            file=sys.stderr,
        )
        return rc_up

    timeout_sec = _restore_recovery_timeout_sec(env)
    try:
        ok, reason = wait_db_logs_for_recovery_ready(
            compose_file, env, timeout_sec=timeout_sec, since=logs_since
        )
    except KeyboardInterrupt:
        print("pgBackRest restore: cancelled while waiting for PostgreSQL recovery.", file=sys.stderr)
        print(
            "The `db` service may still be recovering; check `docker compose logs -f db`.",
            file=sys.stderr,
        )
        return 130
    if not ok:
        print(f"pgBackRest restore: {reason}", file=sys.stderr)
        print(
            "The `db` service may still be recovering; check `docker compose logs -f db`.",
            file=sys.stderr,
        )
        return 1

    print(
        "pgBackRest restore: PostgreSQL finished recovery and is ready to accept connections.",
        file=sys.stderr,
    )
    if config is not None:
        rc_hooks = run_post_db_restore_manage_commands(
            config,
            compose_file=compose_file,
            env_add=env,
            env_name=env_name,
        )
        if rc_hooks != 0:
            return rc_hooks
        if config.has_metabase_fetch():
            rc_mb = run_post_metabase_db_restore_manage_commands(
                config,
                compose_file=compose_file,
                env_add=env,
                env_name=env_name,
            )
            if rc_mb != 0:
                return rc_mb
    return 0


def _log_level_argv_shell(
    env: dict[str, str],
    *,
    for_restore: bool = False,
) -> str:
    """Shell fragment for optional log-level flags inside ``sh -c`` (trailing space if non-empty)."""
    parts: list[str] = []
    v = _resolve_pgbr_console_level(env, for_restore=for_restore)
    if v:
        parts.append(shlex.quote(f"--log-level-console={v}"))
    if (v := (env.get("PGBR_LOG_LEVEL_STDERR") or "").strip()):
        parts.append(shlex.quote(f"--log-level-stderr={v}"))
    if not parts:
        return ""
    return " ".join(parts) + " "
