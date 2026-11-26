"""Restore a ``pg_dump -Fc`` archive into an env's database, on any runtime (M5).

What ``db restore --file/--dumps`` and ``dk transfer`` share. Written once over
``stack_backend.StackBackend``: the compose and quadlet envs differ only in how a file gets into
the db container, how a script runs there, and how the hooks run, which are backend methods.

1. refuse anything that isn't a ``pg_dump -Fc`` archive, before touching anything;
2. make sure db is running; optionally stop the stack's writers (db stays);
3. copy the archive into the db container;
4. replace the database with an empty one, then ``pg_restore`` the archive;
5. remove the copy, start the writers again, then run the post-restore hooks.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from catalpa_tooling.stack_backend import StackBackend

_LABEL = {"app": "app", "metabase": "Metabase"}


def restore_archive(
    b: StackBackend,
    archive: Path,
    extra_pg_restore_args: Sequence[str] = (),
    *,
    target: Literal["app", "metabase"] = "app",
    stop_app: bool = False,
    run_hooks: bool = True,
) -> int:
    """Replace ``b``'s ``target`` database with ``archive``.

    ``stop_app`` stops the stack's writers around the restore (quadlet's ``db restore`` does;
    compose's never has). ``run_hooks`` runs the post-restore hooks once they are back.
    """
    from catalpa_tooling.pgbackrest_db import (
        _pg_restore_compose_inner_script,
        _pg_restore_promote_app_superuser,
        compose_pg_restore_extras_for_config,
        drop_create_app_database_script,
        pg_dump_archive_error,
    )

    config = b.config
    label = _LABEL[target]
    err = pg_dump_archive_error(archive)
    if err:
        print(f"db restore: {err}", file=sys.stderr)
        return 1
    prefix = config.ops.pgbackrest.restore_temp_prefix
    remote = f"/tmp/{prefix}{os.urandom(8).hex()}.dump"
    stopped = b.stop_writers() if stop_app else None
    copied = False
    try:
        rc = b.ensure_db_running()
        if rc != 0:
            return rc
        print(f"db restore: copying {archive} ({archive.stat().st_size} bytes) into db …", file=sys.stderr)
        copied = True  # even a failed copy may leave a file: the cleanup is `rm -f`
        rc = b.copy_into_db(archive, remote)
        if rc != 0:
            print("db restore: copying the archive failed", file=sys.stderr)
            return rc or 1

        print(f"db restore: replacing {label} database with an empty database before restore …", file=sys.stderr)
        postgis = config.native.reset_db.postgis if target == "app" else False
        rc = b.db_sh(drop_create_app_database_script(postgis=postgis, target=target))
        if rc != 0:
            return rc

        extras = compose_pg_restore_extras_for_config(config, list(extra_pg_restore_args))
        restore_as_super = config.native.reset_db.restore_as_super
        inner = _pg_restore_compose_inner_script(
            target,
            extras,
            container_path=remote,
            promote_app_superuser=_pg_restore_promote_app_superuser(
                extras, target=target, restore_as_super=restore_as_super
            ),
            postgis=config.native.reset_db.postgis,
            restore_as_super=restore_as_super,
        )
        print("db restore: pg_restore …", file=sys.stderr)
        rc = b.db_sh(inner)
        if rc != 0:
            return rc
    finally:
        if copied:
            b.db_sh(f"rm -f {shlex.quote(remote)}", root=True)
        if stop_app:
            b.start_writers(stopped)
    if not run_hooks:
        # The caller runs the hooks and reports when the whole job is done (``dk transfer``).
        return 0
    rc = b.run_post_restore_hooks(target)
    if rc == 0:
        print("db restore: done.", file=sys.stderr)
    return rc

