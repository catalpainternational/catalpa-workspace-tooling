"""Shared argparse parent parsers for ``dk`` environment commands."""

from __future__ import annotations

import argparse


def build_env_flags_parent() -> argparse.ArgumentParser:
    """``--dry-run``, ``-y`` / ``--yes``, and ``--tag`` shared by ``dk <env> …``."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print resolved DOCKER_HOST and SITE_ORIGIN, do not run docker compose.",
    )
    parent.add_argument(
        "-y",
        "--yes",
        dest="yes",
        action="store_true",
        help="Skip interactive confirmation for destructive down -v / wipe / restores (non-TTY needs this).",
    )
    parent.add_argument(
        "--tag",
        default=None,
        metavar="TAG",
        help="Override stack image tag (default: image_tag from env info.yaml, else git/images.yaml).",
    )
    return parent
