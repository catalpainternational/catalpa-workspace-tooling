"""Tests for bundled script helpers."""

from catalpa_tooling.script_assets import npm_run_helper_path


def test_npm_run_helper_path_exists() -> None:
    path = npm_run_helper_path()
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "npm_run_in_dir" in text
