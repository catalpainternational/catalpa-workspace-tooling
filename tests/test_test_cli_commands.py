"""``tests backend`` / ``tests frontend`` honour ``native.test`` and the frontend package manager."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from catalpa_tooling import test_cli
from catalpa_tooling.config import load_project_config

_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "minimal_native_project"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    shutil.copytree(_FIXTURE_ROOT, tmp_path, dirs_exist_ok=True)
    (tmp_path / "frontend").mkdir(exist_ok=True)
    (tmp_path / "backend").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record argv passed to run_cmd instead of executing it."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(test_cli, "run_cmd", fake_run)
    # `tests frontend` wraps in nvm only when the host has it; keep the assertion on argv.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/nonexistent-home")))
    return calls


def _use(monkeypatch: pytest.MonkeyPatch, project: Path) -> None:
    config = load_project_config(project)
    monkeypatch.setattr(test_cli, "_config", lambda: config)


def _set_manifest(project: Path, extra: str) -> None:
    manifest = project / "tooling.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + extra, encoding="utf-8")


def test_backend_defaults_to_test_group(
    project: Path, captured: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, project)

    test_cli._run_pytest([])

    assert captured[0][:5] == ["uv", "run", "--group", "test", "pytest"]


def test_backend_group_is_configurable(
    project: Path, captured: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_manifest(project, "\nnative:\n  test:\n    group: dev\n")
    _use(monkeypatch, project)

    test_cli._run_pytest(["-x"])

    assert captured[0] == ["uv", "run", "--group", "dev", "pytest", "-x"]


def test_frontend_defaults_to_npm(
    project: Path, captured: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing consumers with no package manager signal keep the previous npm behaviour."""
    _use(monkeypatch, project)

    test_cli._run_vitest([])

    assert captured[0] == ["npm", "run", "test"]


def test_frontend_uses_pnpm_when_configured(
    project: Path, captured: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_manifest(project, "\nnative:\n  frontend:\n    package_manager: pnpm\n")
    _use(monkeypatch, project)

    test_cli._run_vitest(["--run"])

    assert captured[0] == ["pnpm", "run", "test", "--run"]


def test_frontend_detects_pnpm_from_lockfile(
    project: Path, captured: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / "frontend" / "pnpm-lock.yaml").write_text('lockfileVersion: "9.0"\n', encoding="utf-8")
    _use(monkeypatch, project)

    test_cli._run_vitest([])

    assert captured[0] == ["pnpm", "run", "test"]


def test_frontend_script_is_configurable(
    project: Path, captured: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_manifest(project, "\nnative:\n  test:\n    frontend_script: test:unit\n")
    _use(monkeypatch, project)

    test_cli._run_vitest([])

    assert captured[0] == ["npm", "run", "test:unit"]
