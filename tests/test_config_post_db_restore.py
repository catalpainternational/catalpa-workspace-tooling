"""Tests for ``ops.post_db_restore`` parsing in tooling.yaml."""

from __future__ import annotations

import pytest

from catalpa_tooling.config import ProjectConfigError, _parse_post_db_restore


def test_parse_post_db_restore_defaults() -> None:
    cfg = _parse_post_db_restore(None)
    assert cfg.envs is None
    assert cfg.manage_commands == ()


def test_parse_post_db_restore_argv_lists() -> None:
    cfg = _parse_post_db_restore(
        {
            "envs": ["local", "staging"],
            "manage_commands": [
                ["sync_wagtail_sites", "--profile", "{env_name}"],
                "migrate --noinput",
            ],
        }
    )
    assert cfg.envs == ("local", "staging")
    assert cfg.manage_commands == (
        ("sync_wagtail_sites", "--profile", "{env_name}"),
        ("migrate", "--noinput"),
    )
    assert cfg.applies_to_env("local")
    assert not cfg.applies_to_env("prod")


def test_parse_post_db_restore_rejects_empty_command() -> None:
    with pytest.raises(ProjectConfigError, match="Empty command"):
        _parse_post_db_restore({"manage_commands": [[]]})


def test_parse_post_db_restore_rejects_invalid_entry() -> None:
    with pytest.raises(ProjectConfigError, match="must be a string or list"):
        _parse_post_db_restore({"manage_commands": [123]})
