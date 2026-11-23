"""Restore a ``pg_dump -Fc`` archive into a ``runtime: quadlet`` env's database.

What ``db restore --file/--dumps`` and ``dk transfer`` (into a quadlet env) share. It runs the
same scripts as on compose envs (``drop_create_app_database_script``, the ``pg_restore``
script, the ``ops.post_db_restore`` hooks), through ``podman exec`` over SSH:

1. stop the stack's other units and the edge's socket, so nothing writes while the database is
   replaced (db stays);
2. copy the archive into ``db`` over SSH stdin and check its SHA-256 there;
3. replace the app database with an empty one, then ``pg_restore`` the archive;
4. start the stack again, then run the post-restore hooks (``db_psql``, ``manage.py``).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

#: (ssh argv, local file) → exit code: stream a file to a remote command's stdin.
Uploader = Callable[[list[str], Path], int]


def upload(argv: list[str], path: Path) -> int:
    with open(path, "rb") as f:
        return subprocess.run(argv, stdin=f, check=False).returncode


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def restore_archive(
    b,
    archive: Path,
    extra_pg_restore_args: Sequence[str] = (),
    *,
    stop_app: bool = True,
    run_hooks: bool = True,
    uploader: Uploader = upload,
) -> int:
    """Replace ``b``'s app database with ``archive``. ``b`` is a ``fcos.backend.Backend``."""
    from catalpa_tooling.pgbackrest_db import (
        _pg_restore_compose_inner_script,
        _pg_restore_promote_app_superuser,
        compose_pg_restore_extras_for_config,
        drop_create_app_database_script,
        pg_dump_archive_error,
    )

    config = b.config
    err = pg_dump_archive_error(archive)
    if err:
        print(f"db restore: {err}", file=sys.stderr)
        return 1
    db = config.stack.services.db
    q = shlex.quote
    prefix = config.ops.pgbackrest.restore_temp_prefix
    remote = f"/tmp/{prefix}{os.urandom(8).hex()}.dump"
    # Everything but db, the edge's socket first (else the next request starts caddy again).
    stopped = b.stop_writers() if stop_app else []
    try:
        print(f"db restore: copying {archive} ({archive.stat().st_size} bytes) into {db} …", file=sys.stderr)
        argv = b.host._argv(
            b.login,
            f"podman exec -i -u root {q(db)} sh -c {q(f'umask 077; cat > {q(remote)}')}",
        )
        rc = uploader(argv, archive)
        if rc != 0:
            print("db restore: copying the archive failed", file=sys.stderr)
            return rc or 1
        want = _sha256(archive)
        check = (
            f"chown postgres:postgres {q(remote)} && chmod 600 {q(remote)} && "
            f"sha256sum {q(remote)} | cut -d' ' -f1 | grep -qx {q(want)}"
        )
        rc = b._ssh(f"podman exec -u root {q(db)} sh -c {q(check)}", tty=False)
        if rc != 0:
            print("db restore: the archive in the container doesn't match (SHA-256)", file=sys.stderr)
            return rc or 1

        print("db restore: replacing the app database with an empty one …", file=sys.stderr)
        script = drop_create_app_database_script(postgis=config.native.reset_db.postgis)
        rc = b._ssh(f"podman exec -i {q(db)} sh -c {q(script)}", tty=False)
        if rc != 0:
            return rc

        extras = compose_pg_restore_extras_for_config(config, list(extra_pg_restore_args))
        restore_as_super = config.native.reset_db.restore_as_super
        inner = _pg_restore_compose_inner_script(
            "app",
            extras,
            container_path=remote,
            promote_app_superuser=_pg_restore_promote_app_superuser(
                extras, target="app", restore_as_super=restore_as_super
            ),
            postgis=config.native.reset_db.postgis,
            restore_as_super=restore_as_super,
        )
        print("db restore: pg_restore …", file=sys.stderr)
        rc = b._ssh(f"podman exec -i {q(db)} sh -c {q(inner)}", tty=False)
        if rc != 0:
            return rc
    finally:
        b._ssh(f"podman exec -u root {q(db)} rm -f {q(remote)}", tty=False)
        b.start_units(stopped)
    rc = post_restore_hooks(b, uploader=uploader) if run_hooks else 0
    if rc == 0:
        print("db restore: done.", file=sys.stderr)
    return rc


def post_restore_hooks(b, *, uploader: Uploader = upload) -> int:
    """``ops.post_db_restore`` for a quadlet env: ``db_psql`` SQL piped to psql in ``db``,
    then ``manage.py`` commands in the web container."""
    from catalpa_tooling.post_db_restore import post_db_restore_applies

    if not post_db_restore_applies(b.config, b.env_name):
        return 0
    return _run_hooks(b, b.config.ops.post_db_restore, "post_db_restore", uploader)


def metabase_hooks_apply(config, env_name: str) -> bool:
    hooks = config.ops.post_metabase_db_restore
    return bool(hooks.manage_commands or hooks.db_psql or hooks.restart_services) and hooks.applies_to_env(env_name)


def post_metabase_restore_hooks(b, *, uploader: Uploader = upload) -> int:
    """``ops.post_metabase_db_restore`` for a quadlet env, as compose runs it after a restore that
    brought back Metabase's database (pgBackRest restores the whole cluster): ``db_psql``, then
    ``manage.py`` commands, then restart ``restart_services``."""
    if not metabase_hooks_apply(b.config, b.env_name):
        return 0
    hooks = b.config.ops.post_metabase_db_restore
    rc = _run_hooks(b, hooks, "post_metabase_db_restore", uploader)
    if rc != 0:
        return rc
    for svc in hooks.restart_services:
        print(f"post_metabase_db_restore: restart {svc}", file=sys.stderr)
        rc = b._ssh(f"systemctl --user restart {shlex.quote(svc)}.service", tty=False)
        if rc != 0:
            return rc
    return 0


def _run_hooks(b, hooks, label: str, uploader: Uploader) -> int:
    from catalpa_tooling.pgbackrest_db import _db_shell_vars
    from catalpa_tooling.post_db_restore import _expand_commands
    from catalpa_tooling.site_origin import primary_site_origin_from_info

    config = b.config
    env_add = {"SITE_ORIGIN": primary_site_origin_from_info(b.info)} if b.info else {}
    commands = _expand_commands(
        config, hooks.manage_commands, env_name=b.env_name, env_add=env_add, hook_label=label,
    )  # fmt: skip
    if commands is None:
        return 1
    db = config.stack.services.db
    q = shlex.quote
    for entry in hooks.db_psql:
        vars_ = _db_shell_vars(entry.target)  # type: ignore[arg-type]
        psql = 'export PGPASSWORD="$POSTGRES_PASSWORD"; psql -h 127.0.0.1 -p 5432 -U postgres -d "$APP_DB" -v ON_ERROR_STOP=1 -f '
        print(f"{label}: db_psql target={entry.target} file={entry.file} …", file=sys.stderr)
        if entry.file.startswith("/"):  # a path inside the db image
            rc = b._ssh(f"podman exec {q(db)} sh -c {q(vars_ + psql + q(entry.file))}", tty=False)
        else:
            path = (config.repo_root / entry.file).resolve()
            if not path.is_file():
                print(f"{label}: db_psql file not found: {path}", file=sys.stderr)
                return 1
            argv = b.host._argv(b.login, f"podman exec -i {q(db)} sh -c {q(vars_ + psql + '-')}")
            rc = uploader(argv, path)
        if rc != 0:
            return rc
    for argv in commands:
        print(f"{label}: manage.py {' '.join(argv)}", file=sys.stderr)
        rc = b.manage(list(argv), user=None, tty=False)
        if rc != 0:
            return rc
    return 0
