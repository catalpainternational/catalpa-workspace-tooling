"""Bundled bash helpers for project ``scripts/dev-*.sh`` authors."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def npm_run_helper_path() -> Path:
    """Path to ``share/npm-run.sh`` (``npm_run_in_dir`` function)."""
    return Path(files("catalpa_tooling").joinpath("share/npm-run.sh"))


def package_run_helper_path() -> Path:
    """Path to ``share/package-run.sh`` (``package_run_in_dir`` function)."""
    return Path(files("catalpa_tooling").joinpath("share/package-run.sh"))


def honcho_start_helper_path() -> Path:
    """Path to ``share/honcho-start.sh`` (Honcho supervisor for ``native start``)."""
    return Path(files("catalpa_tooling").joinpath("share/honcho-start.sh"))
