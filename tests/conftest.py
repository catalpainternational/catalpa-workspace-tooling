"""Shared pytest fixtures for catalpa_tooling tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from catalpa_tooling.config import ProjectConfig, load_project_config
from tests.helpers import (
    install_doctl_mocks,
    install_in_memory_sops_mocks,
    write_minimal_tooling_tree,
)

_DOCTL_RESOLUTION_TEST_FILES = frozenset(
    {
        "test_doctl_binary.py",
        "test_doctl_missing.py",
    }
)

# These tests exercise ssh-keyscan / port probing with their own mocks.
_SSH_KNOWN_HOSTS_TEST_FILES = frozenset({"test_ssh_known_hosts.py"})

_MINIMAL_ROOT = Path(__file__).resolve().parent / "fixtures" / "minimal_project"


def _test_file_name(request: pytest.FixtureRequest) -> str | None:
    node_path = getattr(request.node, "path", None) or getattr(request.node, "fspath", None)
    return Path(node_path).name if node_path is not None else None


@pytest.fixture(autouse=True)
def _mock_sops_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Workspace tests do not require a host ``sops`` binary."""
    install_in_memory_sops_mocks(monkeypatch)


@pytest.fixture(autouse=True)
def _mock_doctl_cli(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Workspace tests do not require the official host ``doctl`` binary."""
    if _test_file_name(request) in _DOCTL_RESOLUTION_TEST_FILES:
        return
    install_doctl_mocks(monkeypatch)


@pytest.fixture(autouse=True)
def _block_real_ssh_keyscan(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Fail fast if a test contacts the network via ssh-keyscan (no 120s wait)."""
    if _test_file_name(request) in _SSH_KNOWN_HOSTS_TEST_FILES:
        return

    def _blocked(*_a: object, **_k: object) -> None:
        raise AssertionError(
            "Real SSH known_hosts probing is blocked in tests. "
            "Monkeypatch ensure_ssh_known_host_for_docker_host / "
            "ensure_ssh_known_host_for_ssh_target (or ensure_known_host)."
        )

    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts._ssh_port_open", _blocked)
    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts._run_ssh_keyscan", _blocked)


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
