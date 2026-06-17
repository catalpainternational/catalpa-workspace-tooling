"""Tests for ``native start`` (Honcho supervisor)."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.native_parser import build_native_parser
from catalpa_tooling.native_start import (
    render_default_procfile,
    resolve_native_start_procfile,
    run_native_start,
)
from catalpa_tooling.script_discovery import discover_native_commands
from tests.helpers import write_minimal_tooling_tree


def test_render_default_procfile_migrate_true() -> None:
    text = render_default_procfile(migrate=True)
    assert "uv run native manage migrate" in text
    assert "uv run native runserver" in text
    assert "frontend: uv run native frontend" in text


def test_render_default_procfile_migrate_false() -> None:
    text = render_default_procfile(migrate=False)
    assert "migrate" not in text
    assert text.strip() == "web: uv run native runserver\nfrontend: uv run native frontend"


def test_resolve_auto_generated_procfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_minimal_tooling_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = load_project_config(tmp_path)
    procfile, temp_path = resolve_native_start_procfile(cfg)
    assert temp_path is not None
    assert procfile == temp_path
    content = procfile.read_text(encoding="utf-8")
    assert "uv run native manage migrate" in content
    temp_path.unlink(missing_ok=True)


def test_resolve_custom_procfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_minimal_tooling_tree(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()
    custom = tools / "Procfile"
    custom.write_text("web: echo hi\n", encoding="utf-8")
    tooling = tmp_path / "tooling.yaml"
    tooling.write_text(
        tooling.read_text(encoding="utf-8")
        + "\nnative:\n  start:\n    procfile: tools/Procfile\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = load_project_config(tmp_path)
    procfile, temp_path = resolve_native_start_procfile(cfg)
    assert temp_path is None
    assert procfile == custom.resolve()


def test_resolve_missing_procfile_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_minimal_tooling_tree(tmp_path)
    tooling = tmp_path / "tooling.yaml"
    tooling.write_text(
        tooling.read_text(encoding="utf-8")
        + "\nnative:\n  start:\n    procfile: tools/Procfile\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    cfg = load_project_config(tmp_path)
    with pytest.raises(FileNotFoundError, match="procfile not found"):
        resolve_native_start_procfile(cfg)


def test_run_native_start_invokes_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_tooling_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = load_project_config(tmp_path)
    calls: list[tuple[list[str], Path | None]] = []

    def fake_run_interruptible(cmd, *, cwd=None, **kwargs):
        calls.append((list(cmd), cwd))
        from subprocess import CompletedProcess

        return CompletedProcess(cmd, 0)

    monkeypatch.setattr("catalpa_tooling.native_start.run_interruptible", fake_run_interruptible)
    assert run_native_start(cfg) == 0
    assert len(calls) == 1
    cmd, cwd = calls[0]
    assert cwd == tmp_path
    assert cmd[0] == "bash"
    assert "honcho-start.sh" in cmd[1]
    assert cmd[2].endswith("-Procfile")
    assert cmd[3:] == ["8000", "8080"]


def test_native_parser_has_start_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_tooling_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = load_project_config(tmp_path)
    parser, _ = build_native_parser(cfg)
    ns = parser.parse_args(["start"])
    assert ns.command == "start"
    assert ns.handler == "start"


def test_start_reserved_over_native_start_script(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "native-start.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    found = discover_native_commands(scripts)
    assert "start" not in found
