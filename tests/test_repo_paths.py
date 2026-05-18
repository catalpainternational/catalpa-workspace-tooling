"""Tests for catalpa_tooling.repo_paths."""

from pathlib import Path

import pytest

from catalpa_tooling.repo_paths import repo_root_from_cwd


def test_repo_root_from_cwd_finds_tooling_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_tooling: None
) -> None:
    sub = tmp_path / "backend"
    sub.mkdir()
    (tmp_path / "tooling.yaml").write_text("project:\n  name: test\n", encoding="utf-8")
    monkeypatch.chdir(sub)
    assert repo_root_from_cwd() == tmp_path.resolve()


def test_repo_root_from_cwd_tooling_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    manifest = tmp_path / "custom-tooling.yaml"
    manifest.write_text("project:\n  name: test\n", encoding="utf-8")
    monkeypatch.chdir(nested)
    monkeypatch.setenv("TOOLING_CONFIG", str(manifest))
    assert repo_root_from_cwd() == tmp_path.resolve()


def test_repo_root_from_cwd_raises_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_tooling: None
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="tooling.yaml"):
        repo_root_from_cwd()
