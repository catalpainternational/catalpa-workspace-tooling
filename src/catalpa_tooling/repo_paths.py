"""Locate the application repo root (``tooling.yaml`` manifest)."""

from __future__ import annotations

import os
from pathlib import Path

TOOLING_FILENAME = "tooling.yaml"


def repo_root_from_cwd() -> Path:
    """Walk upward from cwd for a directory containing ``tooling.yaml``.

  When ``TOOLING_CONFIG`` is set, its parent directory is the repo root.
  Raises ``FileNotFoundError`` if no manifest is found.
    """
    env_path = os.environ.get("TOOLING_CONFIG", "").strip()
    if env_path:
        p = Path(env_path).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"TOOLING_CONFIG file not found: {p}")
        return p.parent

    cwd = Path.cwd().resolve()
    for directory in [cwd, *cwd.parents]:
        if (directory / TOOLING_FILENAME).is_file():
            return directory
    raise FileNotFoundError(
        f"Could not find {TOOLING_FILENAME} in {cwd} or any parent directory. "
        "Run commands from the project root or set TOOLING_CONFIG."
    )
