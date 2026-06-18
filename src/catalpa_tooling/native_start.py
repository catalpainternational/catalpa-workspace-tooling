"""Honcho-based ``native start`` (Django + frontend dev servers)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.run_cmd import run_interruptible
from catalpa_tooling.script_assets import honcho_start_helper_path


def render_default_procfile(*, migrate: bool) -> str:
    """Default bero/Wagtail Procfile when ``native.start.procfile`` is unset."""
    if migrate:
        web = "sh -c 'uv run native manage migrate && uv run native runserver'"
    else:
        web = "uv run native runserver"
    return f"web: {web}\nfrontend: uv run native frontend\n"


def resolve_native_start_procfile(cfg: ProjectConfig) -> tuple[Path, Path | None]:
    """Return ``(procfile_path, temp_path)``; ``temp_path`` must be unlinked by caller."""
    start = cfg.native.start
    if start.procfile:
        procfile = cfg.repo_root / start.procfile
        if not procfile.is_file():
            raise FileNotFoundError(f"procfile not found: {procfile}")
        return procfile.resolve(), None

    fd, name = tempfile.mkstemp(prefix="native-start-", suffix="-Procfile")
    os.close(fd)
    temp_path = Path(name)
    temp_path.write_text(
        render_default_procfile(migrate=start.migrate),
        encoding="utf-8",
    )
    return temp_path, temp_path


def run_native_start(cfg: ProjectConfig) -> int:
    """Run Honcho with the project Procfile (or auto-generated default)."""
    temp_path: Path | None = None
    try:
        procfile, temp_path = resolve_native_start_procfile(cfg)
    except FileNotFoundError as exc:
        print(f"native start: {exc}", flush=True)
        return 1

    helper = honcho_start_helper_path()
    if not helper.is_file():
        print(f"native start: bundled helper missing: {helper}", flush=True)
        return 1

    ports = [str(port) for port in cfg.native.start.ports]
    cmd = ["bash", str(helper), str(procfile), *ports]
    try:
        result = run_interruptible(cmd, cwd=cfg.repo_root)
        return int(result.returncode)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
