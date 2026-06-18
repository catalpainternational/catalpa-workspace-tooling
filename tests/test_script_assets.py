"""Tests for bundled script helpers."""

from catalpa_tooling.script_assets import (
    honcho_start_helper_path,
    npm_run_helper_path,
    package_run_helper_path,
)


def test_npm_run_helper_path_exists() -> None:
    path = npm_run_helper_path()
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "npm_run_in_dir" in text
    assert "package_run_in_dir" in text


def test_package_run_helper_path_exists() -> None:
    path = package_run_helper_path()
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "package_run_in_dir" in text


def test_honcho_start_helper_path_exists() -> None:
    path = honcho_start_helper_path()
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "uv run honcho start" in text
    assert "free_ports" in text
