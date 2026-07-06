"""Bundled Caddy bootstrap config for the machine-wide local dev proxy."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def local_proxy_caddyfile_path() -> Path:
    """Path to the shipped bootstrap ``Caddyfile`` for ``catalpa-local-proxy``."""
    return Path(files("catalpa_tooling").joinpath("local_proxy/Caddyfile"))
