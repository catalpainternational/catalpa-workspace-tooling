"""Tests for catalpa_tooling.script_discovery."""

from pathlib import Path

from catalpa_tooling.script_discovery import (
    RESET_DB_POST_SCRIPT,
    discover_dev_commands,
    discover_scripts_commands,
    dev_command_from_script_name,
    reset_db_post_script,
    scripts_command_from_script_name,
)


def test_dev_command_from_script_name() -> None:
    assert dev_command_from_script_name("dev-storybook.sh") == "storybook"
    assert dev_command_from_script_name("dev-reset-db-post.sh") == "reset-db-post"
    assert dev_command_from_script_name("dev-foo_bar.sh") == "foo-bar"
    assert dev_command_from_script_name("fetch_db.sh") is None
    assert dev_command_from_script_name("dev-.sh") is None


def test_scripts_command_from_script_name() -> None:
    assert scripts_command_from_script_name("fetch_db.sh") == "fetch-db"
    assert scripts_command_from_script_name("merge-tetum-transifex.sh") == "merge-tetum-transifex"
    assert scripts_command_from_script_name("dev-storybook.sh") is None


def test_discover_dev_commands_skips_reserved_and_non_dev(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "dev-storybook.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (scripts / "dev-vite.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (scripts / "fetch_db.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    found = discover_dev_commands(scripts)
    assert list(found) == ["storybook"]
    assert found["storybook"].name == "dev-storybook.sh"


def test_discover_scripts_commands_excludes_dev_prefix(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "fetch_db.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (scripts / "dev-storybook.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    found = discover_scripts_commands(scripts)
    assert list(found) == ["fetch-db"]


def test_reset_db_post_script(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    assert reset_db_post_script(scripts) is None
    (scripts / RESET_DB_POST_SCRIPT).write_text("#!/bin/bash\n", encoding="utf-8")
    assert reset_db_post_script(scripts) is not None
