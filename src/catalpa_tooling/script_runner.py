"""Run discovered bash scripts from repo root."""

from __future__ import annotations

import sys
from pathlib import Path

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.run_cmd import run as run_cmd


def run_bash_script(
    config: ProjectConfig,
    script: Path,
    extra: list[str],
    *,
    label: str | None = None,
) -> int:
    """Execute ``bash <script> [extra…]`` with ``cwd=config.repo_root``."""
    if not script.is_file():
        print(f"{label or 'script'}: missing {script}", file=sys.stderr)
        return 1
    cmd = ["bash", str(script), *extra]
    return run_cmd(cmd, cwd=config.repo_root, check=False).returncode
