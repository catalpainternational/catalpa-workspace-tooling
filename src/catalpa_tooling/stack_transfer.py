"""``dk transfer``: copy the app database and media from one env to another, on any runtime (M5).

Written once over ``stack_backend.StackBackend``, so either end can be a compose or a quadlet env:

1. preflight each end (``preflight_transfer``), then confirm by typing the destination's name;
2. stop the destination's writers (db keeps running);
3. database: ``dump_db`` on the source into the local session directory, then
   ``db_archive.restore_archive`` on the destination (no hooks yet);
4. media: ``pull_media`` from the source into the session directory, then ``push_media`` over
   the destination's (``--delete``);
5. start the destination's writers again (also when a step fails), then run its
   ``ops.post_db_restore`` hooks.

Between two compose envs the media keeps compose's own path (``media_transfer``): a direct rsync
between the two hosts when both are reachable, ``--media-method tar``, and the tar fallbacks.
A quadlet end's media moves by rsync only.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.stack_backend import ComposeBackend, StackBackend


def run_transfer(
    config: ProjectConfig,
    *,
    src_b: StackBackend,
    dst_b: StackBackend,
    do_db: bool,
    do_media: bool,
    media_method: str = "rsync",
    dry: bool = False,
    yes: bool = False,
    workdir: str | None = None,
    keep_workdir: bool = False,
) -> int:
    src, dst = src_b.env_name, dst_b.env_name
    both_compose = isinstance(src_b, ComposeBackend) and isinstance(dst_b, ComposeBackend)
    if do_media and media_method != "rsync" and not both_compose:
        print("dk transfer: a quadlet env's media moves by rsync only (--media-method rsync).", file=sys.stderr)
        return 1

    print(f"  source: {src_b.describe()}", file=sys.stderr)
    print(f"  dest:   {dst_b.describe()}", file=sys.stderr)
    print(
        f"  steps: db={do_db} media={do_media}" + (f" media_method={media_method}" if do_media else ""),
        file=sys.stderr,
    )
    # A dry run starts and creates nothing: preflight reports what a real run would.
    errs = [
        *src_b.preflight_transfer(role="source", db=do_db, media=do_media, start_db=not dry),
        *dst_b.preflight_transfer(role="destination", db=do_db, media=do_media, start_db=not dry),
    ]
    if errs:
        print("transfer: preflight failed:", file=sys.stderr)
        for line in errs:
            print(f"  - {line}", file=sys.stderr)
        return 1
    if do_media:
        print(f"  media: {src_b.describe_media()} → {dst_b.describe_media()}", file=sys.stderr)
    print("transfer: preflight ok.", file=sys.stderr)
    if dry:
        print("transfer: dry-run (no changes, no temp dir).", file=sys.stderr)
        return 0

    if not yes:
        if not sys.stdin.isatty():
            print("Refusing transfer without a TTY. Pass --yes if you intend to run non-interactively.", file=sys.stderr)
            return 1
        if not _confirm(src_b, dst_b, do_db=do_db, do_media=do_media, media_method=media_method, both_compose=both_compose):
            print("transfer: cancelled.", file=sys.stderr)
            return 1

    parent = Path(workdir).expanduser() if workdir else Path(config.ops.transfer_workdir)
    parent = parent if parent.is_absolute() else (config.repo_root / parent)
    parent.mkdir(parents=True, exist_ok=True)
    session = Path(tempfile.mkdtemp(prefix=f"{src}_to_{dst}_", dir=str(parent)))
    print(f"transfer: session directory {session}", file=sys.stderr)

    # The destination's writers stay stopped for the whole transfer, and start again whatever
    # happens: a failed step, or Ctrl-C, also while they're being stopped (start_writers takes
    # a None token then).
    stopped = None
    restarted = False
    try:
        stopped = dst_b.stop_writers()
        if do_db:
            rc = _db_leg(src_b, dst_b, session / "pg.dump")
            if rc != 0:
                return rc
        if do_media:
            rc = _media_leg(config, src_b, dst_b, session / "media", media_method, both_compose=both_compose)
            if rc != 0:
                return rc
        restarted = True
        rc = dst_b.start_writers(stopped)
        if rc != 0:
            return rc
        if do_db:
            rc = dst_b.run_post_restore_hooks()
            if rc != 0:
                return rc
    finally:
        if not restarted:
            dst_b.start_writers(stopped)
        if keep_workdir:
            print(f"transfer: kept workdir {session}", file=sys.stderr)
        else:
            shutil.rmtree(session, ignore_errors=True)
    print("transfer: done.", file=sys.stderr)
    return 0


def _confirm(
    src_b: StackBackend,
    dst_b: StackBackend,
    *,
    do_db: bool,
    do_media: bool,
    media_method: str,
    both_compose: bool,
) -> bool:
    from catalpa_tooling.cli_confirm import confirm_by_typing_env_name

    print("WARNING: This will overwrite data on the destination environment:", file=sys.stderr)
    if do_db:
        print("  - PostgreSQL app database (drop + recreate empty DB, then pg_restore)", file=sys.stderr)
    if do_media:
        if media_method == "tar":
            how = "existing files removed, then tar extract"
        elif both_compose:
            how = "rsync --delete (tar fallback on failure)"
        else:
            how = "rsync --delete"
        print(f"  - media ({dst_b.describe_media()}; {how})", file=sys.stderr)
    print(f"  Source: {src_b.env_name}", file=sys.stderr)
    print(f"  Destination: {dst_b.env_name}", file=sys.stderr)
    return confirm_by_typing_env_name(dst_b.env_name)


def _db_leg(src_b: StackBackend, dst_b: StackBackend, dump_path: Path) -> int:
    from catalpa_tooling import db_archive

    try:
        print("transfer: pg_dump (source) …", file=sys.stderr)
        rc = src_b.dump_db(dump_path)
        if rc != 0:
            return rc
        print("transfer: replace the app database (destination) …", file=sys.stderr)
        # The writers are already stopped; the hooks run once they're back.
        return db_archive.restore_archive(dst_b, dump_path, stop_app=False, run_hooks=False)
    finally:
        dump_path.unlink(missing_ok=True)


def _media_leg(
    config: ProjectConfig,
    src_b: StackBackend,
    dst_b: StackBackend,
    media_dir: Path,
    media_method: str,
    *,
    both_compose: bool,
) -> int:
    if both_compose:
        from catalpa_tooling.media_transfer import run_transfer_media

        assert isinstance(src_b, ComposeBackend) and isinstance(dst_b, ComposeBackend)
        return run_transfer_media(
            src_b.env,
            dst_b.env,
            src_media=src_b.media_storage(),
            dst_media=dst_b.media_storage(),
            media_dir=media_dir,
            method=media_method,  # type: ignore[arg-type]
            dry_run=False,
            config=config,
            fallback_tar=media_method == "rsync",
        )
    print("transfer: media → session directory (source) …", file=sys.stderr)
    rc = src_b.pull_media(media_dir)
    if rc != 0:
        return rc
    print("transfer: session directory → media (destination) …", file=sys.stderr)
    return dst_b.push_media(media_dir)
