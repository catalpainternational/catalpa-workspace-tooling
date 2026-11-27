"""Post-restore hooks on a ``runtime: quadlet`` env, and the SSH upload they and the archive
copy use (``Backend.copy_into_db``). Restoring an archive is ``db_archive.restore_archive``."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import sys
from collections.abc import Callable
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
    """One hook set on a quadlet env: ``db_psql`` SQL piped to psql in ``db``, then each
    ``manage.py`` command in a one-shot ``<web>-task`` container (``Backend.run_manage_once``),
    in the order and with the stopping rules compose has (``post_db_restore.run_hook_set``)."""
    from catalpa_tooling.pgbackrest_db import _db_shell_vars
    from catalpa_tooling.post_db_restore import run_hook_set
    from catalpa_tooling.site_origin import primary_site_origin_from_info

    config = b.config
    env_add = {"SITE_ORIGIN": primary_site_origin_from_info(b.info)} if b.info else {}
    db = config.stack.services.db
    q = shlex.quote

    def run_psql(entry) -> int:
        vars_ = _db_shell_vars(entry.target)  # type: ignore[arg-type]
        psql = 'export PGPASSWORD="$POSTGRES_PASSWORD"; psql -h 127.0.0.1 -p 5432 -U postgres -d "$APP_DB" -v ON_ERROR_STOP=1 -f '
        print(f"{label}: db_psql target={entry.target} file={entry.file} …", file=sys.stderr)
        if entry.file.startswith("/"):  # a path inside the db image
            return b._ssh(f"podman exec {q(db)} sh -c {q(vars_ + psql + q(entry.file))}", tty=False)
        path = (config.repo_root / entry.file).resolve()
        if not path.is_file():
            print(f"{label}: db_psql file not found: {path}", file=sys.stderr)
            return 1
        argv = b.host._argv(b.login, f"podman exec -i {q(db)} sh -c {q(vars_ + psql + '-')}")
        return uploader(argv, path)

    return run_hook_set(
        config,
        hooks,
        env_name=b.env_name,
        env_add=env_add,
        hook_label=label,
        run_psql=run_psql,
        run_manage=lambda argv: b.run_manage_once(argv),
    )
