"""Tests for catalpa_tooling.script_discovery."""

from pathlib import Path

from catalpa_tooling.script_discovery import (
    DEPRECATED_RESET_DB_POST_SCRIPT,
    NATIVE_RESET_DB_POST_SCRIPT,
    discover_native_commands,
    discover_scripts_commands,
    dev_command_from_script_name,
    local_command_from_script_name,
    native_command_from_script_name,
    reset_db_post_script,
    scripts_command_from_script_name,
)


def test_native_command_from_script_name() -> None:
    assert native_command_from_script_name("native-storybook.sh") == "storybook"
    assert native_command_from_script_name("native-reset-db-post.sh") == "reset-db-post"
    assert native_command_from_script_name("fetch_db.sh") is None


def test_local_command_from_script_name() -> None:
    assert local_command_from_script_name("local-storybook.sh") == "storybook"
    assert local_command_from_script_name("native-storybook.sh") is None


def test_dev_command_from_script_name() -> None:
    assert dev_command_from_script_name("dev-storybook.sh") == "storybook"
    assert dev_command_from_script_name("dev-reset-db-post.sh") == "reset-db-post"


def test_scripts_command_from_script_name() -> None:
    assert scripts_command_from_script_name("fetch_db.sh") == "fetch-db"
    assert scripts_command_from_script_name("dev-storybook.sh") is None
    assert scripts_command_from_script_name("native-storybook.sh") is None


def test_discover_native_commands_prefers_native_prefix(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "native-storybook.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (scripts / "local-storybook.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (scripts / "dev-storybook.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (scripts / "native-vite.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (scripts / "native-frontend.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    found = discover_native_commands(scripts)
    assert list(found) == ["storybook"]
    assert found["storybook"].name == "native-storybook.sh"
    assert "vite" not in found
    assert "frontend" not in found


def test_discover_native_commands_falls_back_to_local_then_dev(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "local-storybook.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    found = discover_native_commands(scripts)
    assert found["storybook"].name == "local-storybook.sh"

    scripts2 = tmp_path / "scripts2"
    scripts2.mkdir()
    (scripts2 / "dev-storybook.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    found2 = discover_native_commands(scripts2)
    assert found2["storybook"].name == "dev-storybook.sh"


def test_reset_db_post_script(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    assert reset_db_post_script(scripts) == (None, None)
    (scripts / NATIVE_RESET_DB_POST_SCRIPT).write_text("#!/bin/bash\n", encoding="utf-8")
    path, deprecated = reset_db_post_script(scripts)
    assert path is not None
    assert deprecated is None
    (scripts / NATIVE_RESET_DB_POST_SCRIPT).unlink()
    (scripts / DEPRECATED_RESET_DB_POST_SCRIPT).write_text("#!/bin/bash\n", encoding="utf-8")
    path, deprecated = reset_db_post_script(scripts)
    assert deprecated == "dev-"


def test_discover_scripts_commands_merges_dirs_first_wins(tmp_path: Path) -> None:
    primary = tmp_path / "scripts"
    shared = tmp_path / "bero_scripts"
    primary.mkdir()
    shared.mkdir()
    (primary / "fetch_db.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (shared / "fetch_metabase_db.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (shared / "fetch_db.sh").write_text("#!/bin/bash\n", encoding="utf-8")

    found = discover_scripts_commands([primary, shared])
    assert set(found) == {"fetch-db", "fetch-metabase-db"}
    assert found["fetch-db"].parent == primary
    assert found["fetch-metabase-db"].parent == shared


def test_discover_scripts_commands_skips_missing_dir(tmp_path: Path) -> None:
    primary = tmp_path / "scripts"
    primary.mkdir()
    (primary / "helper.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    missing = tmp_path / "nope"

    found = discover_scripts_commands([primary, missing])
    assert list(found) == ["helper"]
