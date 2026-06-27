"""Bundled shell assets (catalpa-direnv.zsh)."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path


def catalpa_direnv_zsh_path() -> Path:
    """Path to the shipped ``catalpa-direnv.zsh`` hook."""
    return Path(files("catalpa_tooling").joinpath("shell/catalpa-direnv.zsh"))
