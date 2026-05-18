"""Bundled systemd unit files and backup scripts (package data)."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def systemd_source_dir() -> Path:
    """Directory containing shipped ``.service``, ``.timer``, and ``.sh`` assets."""
    return Path(files("catalpa_tooling").joinpath("systemd"))
