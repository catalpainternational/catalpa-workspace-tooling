"""``dk transfer`` when either end is a ``runtime: quadlet`` env.

The same steps as between two compose envs (``dk_transfer.cmd_transfer``), with each end
done the way its runtime needs:

=================  ============================================  ===============================
step               quadlet end                                   compose end
=================  ============================================  ===============================
writers            stop every unit but db (edge socket first)    ``compose stop`` django/caddy
database out       ``pg_dump`` through ``podman exec``           ``compose exec db pg_dump``
database in        drop/create + ``pg_restore`` (fcos.dbdump)    the same scripts via compose
media out          rsync from the data volume's directory        rsync or tar from the volume
media in           rsync under ``podman unshare``, chowned to    rsync (tar fallback)
                   the media's ID range
post-restore       ``ops.post_db_restore`` hooks via podman      the same via compose
=================  ============================================  ===============================

Media always goes through the local session directory (``--media-method rsync`` only).
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from catalpa_tooling.config import ProjectConfig


def run_transfer(
    config: ProjectConfig,
    *,
    src: str,
    dst: str,
    src_b,
    dst_b,
    do_db: bool,
    do_media: bool,
    media_method: str,
    dry: bool,
    yes: bool,
    workdir: str | None,
    keep_workdir: bool,
) -> int:
    """``src_b``/``dst_b``: the end's ``fcos.backend.Backend``, or None for a compose env."""
    from catalpa_tooling.dk_transfer import _start_dest_writers, _stop_dest_writers
    from catalpa_tooling.fcos.backend import Usage
    from catalpa_tooling.managed_deploy_env import load_managed_deploy_context
    from catalpa_tooling.media_storage import MediaStorageError, resolve_media_storage
    from catalpa_tooling.restic_files import resolve_env_with_compose_project

    if do_media and media_method != "rsync":
        print("dk transfer: a quadlet env's media moves by rsync only (--media-method rsync).", file=sys.stderr)
        return 1

    # The compose end(s): the usual context, decrypted.
    ctx = {}
    for name, b in ((src, src_b), (dst, dst_b)):
        if b is None:
            c = load_managed_deploy_context(config, name)
            if c is None:
                return 1
            env = resolve_env_with_compose_project(c.compose_file, c.env_add, config=config, dk_env_name=name)
            media = None
            if do_media:
                try:
                    media = resolve_media_storage(compose_file=c.compose_file, env=env, config=config)
                except MediaStorageError as exc:
                    print(f"dk transfer: {exc}", file=sys.stderr)
                    return 1
            ctx[name] = (c, env, media)

    def describe(name, b) -> str:
        if b is not None:
            where = f"quadlet on {b.ssh_target}"
            if do_media:
                where += f" media={b.media_dir()}"
            return where
        c, _env, media = ctx[name]
        return f"compose={c.compose_file!r} DOCKER_HOST={c.docker_host!r}" + (
            f" media={media.describe()}" if media else ""
        )

    try:
        print(f"  source: {src} {describe(src, src_b)}", file=sys.stderr)
        print(f"  dest:   {dst} {describe(dst, dst_b)}", file=sys.stderr)
    except Usage as exc:
        print(f"transfer: preflight failed: {exc}", file=sys.stderr)
        return 1
    print(f"  steps: db={do_db} media={do_media}", file=sys.stderr)
    for name, b in ((src, src_b), (dst, dst_b)):
        if b is not None and config.stack.services.db not in b.services and do_db:
            print(f"transfer: preflight failed: {name}'s render has no db service", file=sys.stderr)
            return 1
    print("transfer: preflight ok.", file=sys.stderr)
    if dry:
        print("transfer: dry-run (no changes, no temp dir).", file=sys.stderr)
        return 0

    if not yes:
        if not sys.stdin.isatty():
            print("Refusing transfer without a TTY. Pass --yes if you intend to run non-interactively.", file=sys.stderr)
            return 1
        from catalpa_tooling.cli_confirm import confirm_by_typing_env_name

        what = " and ".join(w for w, on in (("app database", do_db), ("media", do_media)) if on)
        print(f"WARNING: this replaces {dst}'s {what} with {src}'s.", file=sys.stderr)
        if not confirm_by_typing_env_name(dst):
            print("transfer: cancelled.", file=sys.stderr)
            return 1

    parent = Path(workdir).expanduser() if workdir else config.repo_root / config.ops.transfer_workdir
    parent = parent if parent.is_absolute() else (config.repo_root / parent)
    parent.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix=f"{src}_to_{dst}_", dir=str(parent)))
    print(f"transfer: session directory {session}", file=sys.stderr)
    dump_path = session / "pg.dump"
    media_dir = session / "media"

    # Writers on the destination stop for the whole transfer.
    restarted = False
    if dst_b is not None:
        stopped = dst_b.stop_writers()

        def restart() -> int:
            return dst_b.start_units(stopped)
    else:
        c, env, _media = ctx[dst]
        _stop_dest_writers(c.compose_file, env, dry_run=False)

        def restart() -> int:
            _start_dest_writers(c.compose_file, env, dry_run=False, config=config, env_name=dst, info=c.info)
            return 0

    try:
        if do_db:
            rc = _db_leg(config, src, dst, src_b, dst_b, ctx, dump_path)
            if rc != 0:
                return rc
        if do_media:
            rc = _media_leg(config, src, dst, src_b, dst_b, ctx, media_dir)
            if rc != 0:
                return rc
        restarted = True
        rc = restart()
        if rc != 0:
            return rc
        if do_db:
            rc = _hooks(config, dst, dst_b, ctx)
            if rc != 0:
                return rc
    finally:
        if not restarted:
            restart()
        if not keep_workdir:
            shutil.rmtree(session, ignore_errors=True)
        else:
            print(f"transfer: kept workdir {session}", file=sys.stderr)
    print("transfer: done.", file=sys.stderr)
    return 0


def _db_leg(config, src, dst, src_b, dst_b, ctx, dump_path: Path) -> int:
    from catalpa_tooling.fcos.dbdump import restore_archive
    from catalpa_tooling.fcos.fetch import fetch_db
    from catalpa_tooling.pgbackrest_db import (
        compose_pg_restore_extras_for_config,
        run_drop_create_app_database,
        run_pg_dump_to_file,
        run_pg_restore,
    )

    print("transfer: pg_dump (source) …", file=sys.stderr)
    if src_b is not None:
        try:
            fetch_db(config, src_b, dest=dump_path)
        except SystemExit as exc:
            return int(exc.code or 1)
    else:
        c, env, _m = ctx[src]
        rc = run_pg_dump_to_file(c.compose_file, env, dump_path)
        if rc != 0:
            return rc

    print("transfer: replace the app database (destination) …", file=sys.stderr)
    try:
        if dst_b is not None:
            # The writers are already stopped; the hooks run once they're back.
            return restore_archive(dst_b, dump_path, stop_app=False, run_hooks=False)
        c, env, _m = ctx[dst]
        rc = run_drop_create_app_database(c.compose_file, env, postgis=config.native.reset_db.postgis)
        if rc != 0:
            return rc
        return run_pg_restore(
            c.compose_file,
            env,
            compose_pg_restore_extras_for_config(config, ["--file", str(dump_path)]),
            config=config,
        )
    finally:
        dump_path.unlink(missing_ok=True)


def _media_leg(config, src, dst, src_b, dst_b, ctx, media_dir: Path) -> int:
    from catalpa_tooling.media_rsync import resolve_rsync_endpoint, run_push_media_rsync
    from catalpa_tooling.media_transfer import _stage_source_to_media_dir

    media_dir.mkdir(parents=True, exist_ok=True)
    print("transfer: media → session directory (source) …", file=sys.stderr)
    if src_b is not None:
        rc = src_b.pull_media(media_dir)
    else:
        _c, env, media = ctx[src]
        rc = _stage_source_to_media_dir(
            env,
            src_media=media,
            src_endpoint=resolve_rsync_endpoint(env, media, label="transfer source", config=config),
            media_dir=media_dir,
            dry_run=False,
            config=config,
            alpine_image="alpine:3.21",
        )
    if rc != 0:
        return rc
    print("transfer: session directory → media (destination) …", file=sys.stderr)
    if dst_b is not None:
        return dst_b.push_media(media_dir)
    _c, env, media = ctx[dst]
    return run_push_media_rsync(
        env, source=media_dir, dry_run=False, method="rsync", delete=True,
        config=config, storage=media, label="transfer push",
    )  # fmt: skip


def _hooks(config, dst, dst_b, ctx) -> int:
    if dst_b is not None:
        from catalpa_tooling.fcos.dbdump import post_restore_hooks

        return post_restore_hooks(dst_b)
    from catalpa_tooling.post_db_restore import run_post_db_restore_manage_commands

    c, env, _m = ctx[dst]
    return run_post_db_restore_manage_commands(config, compose_file=c.compose_file, env_add=env, env_name=dst)
