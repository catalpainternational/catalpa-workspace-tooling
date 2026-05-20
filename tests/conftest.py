"""Shared pytest fixtures for catalpa_tooling tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from catalpa_tooling.config import ProjectConfig, load_project_config
from tests.helpers import install_in_memory_sops_mocks, write_minimal_tooling_tree

_MINIMAL_ROOT = Path(__file__).resolve().parent / "fixtures" / "minimal_project"


@pytest.fixture(autouse=True)
def _mock_sops_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Workspace tests do not require a host ``sops`` binary."""
    install_in_memory_sops_mocks(monkeypatch)


@pytest.fixture
def isolated_tooling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset ``TOOLING_CONFIG`` so ``load_project_config(tmp_path)`` uses that tree."""
    monkeypatch.delenv("TOOLING_CONFIG", raising=False)


@pytest.fixture(scope="session")
def minimal_config() -> ProjectConfig:
    """``ProjectConfig`` for the committed minimal_project fixture directory."""
    prev = os.environ.pop("TOOLING_CONFIG", None)
    try:
        return load_project_config(_MINIMAL_ROOT)
    finally:
        if prev is not None:
            os.environ["TOOLING_CONFIG"] = prev


@pytest.fixture
def minimal_project(tmp_path: Path, isolated_tooling: None) -> ProjectConfig:
    """Copy minimal_project fixture tree into ``tmp_path`` and load ``ProjectConfig``."""
    write_minimal_tooling_tree(tmp_path)
    return load_project_config(tmp_path)
