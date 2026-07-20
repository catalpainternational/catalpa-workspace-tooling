"""Orchestrate ``django_media`` copy for ``dk transfer`` (rsync preferred, tar fallback)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.media_pull import run_pull_media, run_push_media
from catalpa_tooling.media_rsync import (
    TransferMediaMethod,
    resolve_rsync_endpoint,
    rsync_between_endpoints,
    run_push_media_rsync,
)
from catalpa_tooling.media_storage import MediaStorage


def _transfer_media_tar(
    src_env: dict[str, str],
    dst_env: dict[str, str],
    *,
    src_media: MediaStorage,
    dst_media: MediaStorage,
    media_dir: Path,
    dry_run: bool,
    config: ProjectConfig | None,
    alpine_image: str,
) -> int:
    print(f"transfer: pull_media tar (source {src_media.describe()}) …", file=sys.stderr)
    rc = run_pull_media(
        src_env,
        target=media_dir,
        dry_run=dry_run,
        alpine_image=alpine_image,
        config=config,
        storage=src_media,
    )
    if rc != 0:
        return rc
    print(f"transfer: push_media tar (destination {dst_media.describe()}) …", file=sys.stderr)
    return run_push_media(
        dst_env,
        source=media_dir,
        dry_run=dry_run,
        alpine_image=alpine_image,
        config=config,
        storage=dst_media,
    )


def _stage_source_to_media_dir(
    src_env: dict[str, str],
    *,
    src_media: MediaStorage,
    src_endpoint: str | None,
    media_dir: Path,
    dry_run: bool,
    config: ProjectConfig | None,
    alpine_image: str,
) -> int:
    """Populate ``media_dir`` from the source (rsync when host-reachable, else tar)."""
    if src_endpoint is not None:
        print(
            f"transfer: stage via rsync ({src_endpoint} → {media_dir}) …",
            file=sys.stderr,
        )
        if dry_run:
            print(
                f"dry-run: rsync {src_endpoint}/ → {media_dir}/",
                file=sys.stderr,
            )
            return 0
        media_dir.mkdir(parents=True, exist_ok=True)
        return rsync_between_endpoints(
            src_endpoint,
            str(media_dir),
            delete=True,
            dry_run=False,
        )

    print(f"transfer: stage via tar pull (source {src_media.describe()}) …", file=sys.stderr)
    return run_pull_media(
        src_env,
        target=media_dir,
        dry_run=dry_run,
        alpine_image=alpine_image,
        config=config,
        storage=src_media,
    )


def run_transfer_media(
    src_env: dict[str, str],
    dst_env: dict[str, str],
    *,
    src_media: MediaStorage,
    dst_media: MediaStorage,
    media_dir: Path,
    method: TransferMediaMethod = "rsync",
    dry_run: bool = False,
    config: ProjectConfig | None = None,
    alpine_image: str = "alpine:3.21",
    fallback_tar: bool = True,
) -> int:
    """Copy media from source env storage to destination (rsync default, tar fallback)."""
    if method == "tar":
        return _transfer_media_tar(
            src_env,
            dst_env,
            src_media=src_media,
            dst_media=dst_media,
            media_dir=media_dir,
            dry_run=dry_run,
            config=config,
            alpine_image=alpine_image,
        )

    if not shutil.which("rsync"):
        print(
            "transfer: rsync is not on PATH; falling back to tar.",
            file=sys.stderr,
        )
        if not fallback_tar:
            return 1
        return _transfer_media_tar(
            src_env,
            dst_env,
            src_media=src_media,
            dst_media=dst_media,
            media_dir=media_dir,
            dry_run=dry_run,
            config=config,
            alpine_image=alpine_image,
        )

    src_ep = resolve_rsync_endpoint(
        src_env, src_media, label="transfer source", config=config
    )
    dst_ep = resolve_rsync_endpoint(
        dst_env, dst_media, label="transfer destination", config=config
    )

    if src_ep is not None and dst_ep is not None:
        print(
            f"transfer: media rsync direct ({src_ep} → {dst_ep}) …",
            file=sys.stderr,
        )
        rc = rsync_between_endpoints(src_ep, dst_ep, delete=True, dry_run=dry_run)
        if rc == 0 or dry_run:
            return rc
        if fallback_tar:
            print(
                "transfer: direct rsync failed; falling back to tar pull+push.",
                file=sys.stderr,
            )
            return _transfer_media_tar(
                src_env,
                dst_env,
                src_media=src_media,
                dst_media=dst_media,
                media_dir=media_dir,
                dry_run=False,
                config=config,
                alpine_image=alpine_image,
            )
        return rc

    print(
        "transfer: media rsync stage+push "
        f"(src_endpoint={'yes' if src_ep else 'no'}, "
        f"dst_endpoint={'yes' if dst_ep else 'no'}) …",
        file=sys.stderr,
    )
    rc = _stage_source_to_media_dir(
        src_env,
        src_media=src_media,
        src_endpoint=src_ep,
        media_dir=media_dir,
        dry_run=dry_run,
        config=config,
        alpine_image=alpine_image,
    )
    if rc != 0:
        if fallback_tar and not dry_run and src_ep is not None:
            print(
                "transfer: stage rsync failed; falling back to tar pull+push.",
                file=sys.stderr,
            )
            return _transfer_media_tar(
                src_env,
                dst_env,
                src_media=src_media,
                dst_media=dst_media,
                media_dir=media_dir,
                dry_run=False,
                config=config,
                alpine_image=alpine_image,
            )
        return rc

    print(f"transfer: push_media rsync (destination {dst_media.describe()}) …", file=sys.stderr)
    rc = run_push_media_rsync(
        dst_env,
        source=media_dir,
        dry_run=dry_run,
        method="rsync",
        delete=True,
        alpine_image=alpine_image,
        config=config,
        storage=dst_media,
        label="transfer push",
    )
    if rc == 0 or dry_run:
        return rc
    if fallback_tar:
        print(
            "transfer: rsync push failed; falling back to tar push.",
            file=sys.stderr,
        )
        return run_push_media(
            dst_env,
            source=media_dir,
            dry_run=False,
            alpine_image=alpine_image,
            config=config,
            storage=dst_media,
        )
    return rc
