"""Run discovered bash scripts from repo root."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from catalpa_tooling.deprecation import warn_deprecated
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.ssh_known_hosts import ensure_ssh_known_host_for_ssh_target


def script_process_env(config: ProjectConfig) -> dict[str, str]:
    """Environment variables for ``scripts/*.sh`` and shared deploy helpers."""
    env = os.environ.copy()
    env["CATALPA_REPO_ROOT"] = str(config.repo_root)
    env["CATALPA_FRONTEND_DIR"] = str(config.frontend_dir.relative_to(config.repo_root))
    if config.native.fetch.ssh_host:
        env["FETCH_DB_SSH_HOST"] = config.native.fetch.ssh_host
    elif config.native.fetch_metabase_db.ssh_host:
        env["FETCH_DB_SSH_HOST"] = config.native.fetch_metabase_db.ssh_host
    dump = config.fetch_metabase_db_dump_path
    env["FETCH_DB_OUTPUT"] = str(dump)
    ssh_host = (env.get("FETCH_DB_SSH_HOST") or "").strip()
    if ssh_host and ensure_ssh_known_host_for_ssh_target(ssh_host) != 0:
        raise SystemExit(1)
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
    name = script.name
    if name == "fetch_db.sh":
        warn_deprecated("scripts fetch-db", "dk fetch db")
    elif name == "fetch_metabase_db.sh":
        warn_deprecated("scripts fetch-metabase-db", "dk fetch db --only metabase")
    cmd = ["bash", str(script), *extra]
    return run_cmd(
        cmd,
        cwd=config.repo_root,
        env=script_process_env(config),
        check=False,
    ).returncode
