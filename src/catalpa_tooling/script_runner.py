"""Run discovered bash scripts from repo root."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.run_cmd import run as run_cmd


def script_process_env(config: ProjectConfig) -> dict[str, str]:
    """Environment variables for ``scripts/*.sh`` and shared bero helpers."""
    env = os.environ.copy()
    env["CATALPA_REPO_ROOT"] = str(config.repo_root)
    env["CATALPA_FRONTEND_DIR"] = str(config.frontend_dir.relative_to(config.repo_root))
    if config.native.fetch_metabase_db.ssh_host:
        env["FETCH_DB_SSH_HOST"] = config.native.fetch_metabase_db.ssh_host
    dump = config.fetch_metabase_db_dump_path
    if dump is not None:
        env["FETCH_DB_OUTPUT"] = str(dump)
    return env


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
    return run_cmd(
        cmd,
        cwd=config.repo_root,
        env=script_process_env(config),
        check=False,
    ).returncode
