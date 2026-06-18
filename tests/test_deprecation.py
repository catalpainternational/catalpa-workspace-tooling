"""Tests for deprecation warnings."""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.deprecation import warn_deprecated
from catalpa_tooling.remote_deploy import resolve_deploy_env_name
from tests.test_fetch_media_config import _write_minimal_tooling


def test_warn_deprecated_prints_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    warn_deprecated("dev", "native", context="tooling.yaml")
    captured = capsys.readouterr()
    assert "Deprecated: use `native` instead of `dev`." in captured.err
    assert "(tooling.yaml)" in captured.err
    assert captured.out == ""


def test_yaml_dev_fallback_warns(tmp_path: Path, isolated_tooling: None, capsys: pytest.CaptureFixture[str]) -> None:
    _write_minimal_tooling(tmp_path)
    (tmp_path / "tooling.yaml").write_text(
        (tmp_path / "tooling.yaml").read_text(encoding="utf-8")
        + """
dev:
  reset_db:
    postgis: true
""",
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    captured = capsys.readouterr()
    assert "Deprecated: use `native:` instead of `dev:`." in captured.err
    assert cfg.native.reset_db.postgis is True


def test_yaml_local_fallback_warns(tmp_path: Path, isolated_tooling: None, capsys: pytest.CaptureFixture[str]) -> None:
    _write_minimal_tooling(tmp_path)
    (tmp_path / "tooling.yaml").write_text(
        (tmp_path / "tooling.yaml").read_text(encoding="utf-8")
        + """
local:
  reset_db:
    postgis: true
""",
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    captured = capsys.readouterr()
    assert "Deprecated: use `native:` instead of `local:`." in captured.err
    assert cfg.native.reset_db.postgis is True


def test_env_alias_warns(tmp_path: Path, isolated_tooling: None, capsys: pytest.CaptureFixture[str]) -> None:
    _write_minimal_tooling(tmp_path)
    (tmp_path / "tooling.yaml").write_text(
        (tmp_path / "tooling.yaml").read_text(encoding="utf-8").replace(
            "    dev_compose: compose.dev.yaml",
            "    dev_compose: compose.dev.yaml\n    env_aliases:\n      local: full",
        ),
        encoding="utf-8",
    )
    env_dir = tmp_path / "docker" / "envs" / "full"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("docker_host: ssh://u@h\n", encoding="utf-8")
    cfg = load_project_config(tmp_path)
    assert resolve_deploy_env_name(cfg, "local") == "full"
    captured = capsys.readouterr()
    assert "Deprecated: use `dk full` instead of `dk local`." in captured.err


def test_native_cli_dev_entry_point_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_tooling: None
) -> None:
    from catalpa_tooling.native_cli import main

    _write_minimal_tooling(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["dev", "--help"])
    stderr = StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert "Deprecated: use `native` instead of `dev`." in stderr.getvalue()


def test_native_cli_local_entry_point_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_tooling: None
) -> None:
    from catalpa_tooling.native_cli import main

    _write_minimal_tooling(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["local", "--help"])
    stderr = StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert "Deprecated: use `native` instead of `local`." in stderr.getvalue()
