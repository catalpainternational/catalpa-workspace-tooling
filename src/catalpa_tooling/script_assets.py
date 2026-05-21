"""Bundled bash helpers for project ``scripts/dev-*.sh`` authors."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def npm_run_helper_path() -> Path:
    """Path to ``share/npm-run.sh`` (``npm_run_in_dir`` function)."""
    return Path(files("catalpa_tooling").joinpath("share/npm-run.sh"))
