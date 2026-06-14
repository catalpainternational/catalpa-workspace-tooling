"""Tests for ``dev.reset_db`` parsing in tooling.yaml."""

from __future__ import annotations

import pytest

from catalpa_tooling.config import (
    ProjectConfigError,
    ResetDbConfig,
    _parse_native,
    _parse_reset_db,
)


def test_parse_reset_db_defaults() -> None:
    cfg = _parse_reset_db(None)
    assert cfg.postgis is False
    assert cfg.pg_restore_args == ("--clean", "--if-exists")
    assert cfg.post_manage_commands == ()
    assert cfg.db_name_env == ("DJANGO_DB_NAME", "DJANGO_DB", "POSTGRES_DB")
    assert cfg.db_name_fallback is None


def test_parse_reset_db_catalpa_style() -> None:
    cfg = _parse_reset_db(
        {
            "postgis": True,
            "db_name_fallback": "catalpa_db",
            "db_name_env": ["DJANGO_DB", "POSTGRES_DB"],
            "pg_restore_args": ["--clean", "--if-exists"],
            "post_manage_commands": [["sync_wagtail_sites", "--profile", "host"]],
        }
    )
    assert cfg.postgis is True
    assert cfg.db_name_fallback == "catalpa_db"
    assert cfg.pg_restore_args == ("--clean", "--if-exists")
    assert cfg.post_manage_commands == (("sync_wagtail_sites", "--profile", "host"),)


def test_parse_native_includes_reset_db() -> None:
    local_cfg = _parse_native({"fetch_media": {"dk_env": "prod"}, "reset_db": {"postgis": False}})
    assert local_cfg.reset_db.postgis is False
    assert local_cfg.reset_db.pg_restore_args == ("--clean", "--if-exists")
    assert isinstance(local_cfg.reset_db, ResetDbConfig)


def test_parse_reset_db_rejects_empty_command() -> None:
    with pytest.raises(ProjectConfigError, match="Empty command"):
        _parse_reset_db({"post_manage_commands": [[]]})
