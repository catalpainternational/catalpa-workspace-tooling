"""pgBackRest helpers for ``dk <env> bkp_db`` (see README_PGBACKREST.md)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.pgbackrest_volume_config import (
    conflict_error_message,
    ensure_postgres_data_volume,
    resolve_mode,
    stanza_from_env_for_pgbackrest,
)
from catalpa_tooling.cli_confirm import confirm_by_typing_env_name
from catalpa_tooling.post_db_restore import run_post_db_restore_manage_commands
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.run_cmd import run_interruptible

BackupType = Literal["full", "incr", "diff"]

# First bytes of ``pg_dump -Fc`` custom format (must not be preceded by other stdout).
_PG_DUMP_CUSTOM_MAGIC = b"PGDMP"

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
        'export PGPASSWORD="$POSTGRES_PASSWORD"; '
        'exec pg_dump -h 127.0.0.1 -p 5432 -U postgres -d "$DJANGO_APP_DB" -Fc'
    )
    if extras:
        inner = inner + " " + " ".join(shlex.quote(a) for a in extras)
    return inner


def run_pg_dump(
    compose_file: str,
    env: dict[str, str],
    extra_pg_dump_args: Sequence[str] | None = None,
) -> int:
    """Run ``pg_dump`` in the ``db`` container against the app DB (``DJANGO_APP_DB``); stream to stdout.

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


def run_drop_create_app_database(
    compose_file: str,
    env: dict[str, str],
) -> int:
    """Replace the Django app database with an empty one (same layout as ``docker/postgres/init/01-init.sh`` grants).

    Runs ``dropdb --force`` (PostgreSQL 13+) so existing connections are terminated, then
    ``createdb -O "$DJANGO_APP_DB_USER"``. Used by ``dk transfer`` instead of ``pg_restore --clean``,
    which issues ``DROP`` statements in an order that can fail when FKs reference constraints being
    dropped.
    """
    merged = _merged_process_env(env)
    script = """set -eu
export PGPASSWORD="$POSTGRES_PASSWORD"
dropdb -h 127.0.0.1 -p 5432 -U postgres --if-exists --force "$DJANGO_APP_DB"
createdb -h 127.0.0.1 -p 5432 -U postgres -O "$DJANGO_APP_DB_USER" "$DJANGO_APP_DB"
psql -h 127.0.0.1 -p 5432 -U postgres -d "$DJANGO_APP_DB" -v ON_ERROR_STOP=1 <<EOF
GRANT ALL PRIVILEGES ON DATABASE ${DJANGO_APP_DB} TO ${DJANGO_APP_DB_USER};
CREATE EXTENSION IF NOT EXISTS postgis;
GRANT ALL ON SCHEMA public TO ${DJANGO_APP_DB_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO ${DJANGO_APP_DB_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO ${DJANGO_APP_DB_USER};
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


def run_pg_restore(
    compose_file: str,
    env: dict[str, str],
    extra_pg_restore_args: Sequence[str] | None = None,
) -> int:
    """Run ``pg_restore`` in the ``db`` container against ``DJANGO_APP_DB``.

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
        container_path = f"/tmp/indmo_pgrestore_{os.urandom(8).hex()}.dump"

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

        inner = (
            'export PGPASSWORD="$POSTGRES_PASSWORD"; '
            'exec pg_restore -h 127.0.0.1 -p 5432 -U postgres -d "$DJANGO_APP_DB"'
        )
        if extras:
            inner = inner + " " + " ".join(shlex.quote(a) for a in extras)
        inner = inner + " " + shlex.quote(container_path)
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
    return run_cmd(
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
    ).returncode


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
    return run_cmd(
        argv,
        env=_merged_process_env(env),
        stdin=subprocess.DEVNULL,
        check=False,
    ).returncode


def _remove_interrupted_compose_run_db(compose_file: str, env: dict[str, str]) -> None:
    """Remove one-off ``db`` containers left when ``compose run`` is interrupted."""
    merged = _merged_process_env(env)
    filters = [
        "label=com.docker.compose.oneoff=True",
        "label=com.docker.compose.service=db",
    ]
    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip()
    if project:
        filters.append(f"label=com.docker.compose.project={project}")
    ps_argv = ["docker", "ps", "-q"]
    for f in filters:
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
    if not ids:
        return
    print(
        "pgBackRest restore: removing interrupted one-off `db` container(s)…",
        file=sys.stderr,
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
    try:
        if ready.wait(timeout=timeout_sec):
            return True, ""
        if proc.poll() is not None:
            return (
                False,
                "`docker compose logs -f db` exited before PostgreSQL reported recovery complete.",
            )
        return (
            False,
            f"timed out after {timeout_sec}s waiting for `{_PG_RECOVERY_READY_LOG}` in `db` logs.",
        )
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        th.join(timeout=5)


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

    if ensure_postgres_data_volume(env, config=config) != 0:
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

    extras = tuple(extra_pgbackrest_args or ())
    extra_shell = " ".join(shlex.quote(a) for a in extras)
    inner = (
        f"pgbackrest {_log_level_argv_shell(env, for_restore=True)}"
        f"--stanza={shlex.quote(stanza)} restore --delta"
    )
    if extra_shell:
        inner = f"{inner} {extra_shell}"

    restore_argv = [
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
    if rc_up != 0:
        print(
            f"pgBackRest restore: `docker compose up -d --force-recreate db` failed (exit {rc_up}). "
            "The data directory was restored; start `db` manually when ready.",
            file=sys.stderr,
        )
        return rc_up

    timeout_sec = _restore_recovery_timeout_sec(env)
    ok, reason = wait_db_logs_for_recovery_ready(
        compose_file, env, timeout_sec=timeout_sec, since=logs_since
    )
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
