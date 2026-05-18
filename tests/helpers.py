"""Shared helpers for catalpa_tooling tests (not collected as tests)."""

from __future__ import annotations

import shutil
from pathlib import Path

_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "minimal_project"


def write_minimal_tooling_tree(target: Path) -> None:
    """Lay down ``tooling.yaml``, ``pyproject.toml``, and ``docker/images.yaml`` under ``target``."""
    shutil.copytree(_FIXTURE_ROOT, target, dirs_exist_ok=True)
    (target / "pyproject.toml").write_text('name = "minimal-test"\n', encoding="utf-8")
    (target / "docker").mkdir(parents=True, exist_ok=True)
    (target / "docker" / "images.yaml").write_text(
        "image_registry: ghcr.io/example/app\n",
        encoding="utf-8",
    )
