"""Tests for tooling.yaml + info.yaml backup logging env merge."""

from __future__ import annotations

import pytest

from catalpa_tooling.backup_logging_env import merge_backup_logging_env
from catalpa_tooling.backup_logging_levels import BackupLoggingConfigError
from catalpa_tooling.config import load_project_config


def test_merge_tooling_defaults(minimal_project) -> None:
    cfg = load_project_config(minimal_project.repo_root)
    merged = merge_backup_logging_env(cfg, None)
    assert merged["PGBR_RESTORE_LOG_LEVEL_CONSOLE"] == "detail"
    assert merged["RESTIC_RESTORE_VERBOSE"] == "1"
    assert "PGBR_LOG_LEVEL_CONSOLE" not in merged
    assert "RESTIC_VERBOSE" not in merged


def test_info_overrides_tooling(minimal_project) -> None:
    cfg = load_project_config(minimal_project.repo_root)
    info = {
        "pgbackrest": {"log_level_console": "detail"},
        "restic": {"restore_verbose": 3},
    }
    merged = merge_backup_logging_env(cfg, info)
    assert merged["PGBR_LOG_LEVEL_CONSOLE"] == "detail"
    assert merged["PGBR_RESTORE_LOG_LEVEL_CONSOLE"] == "detail"
    assert merged["RESTIC_RESTORE_VERBOSE"] == "3"


def test_info_env_block_wins(minimal_project) -> None:
    cfg = load_project_config(minimal_project.repo_root)
    info = {
        "pgbackrest": {"log_level_console": "warn"},
        "env": {"pgbr_log_level_console": "debug"},
    }
    merged = merge_backup_logging_env(cfg, info)
    assert merged["PGBR_LOG_LEVEL_CONSOLE"] == "debug"


def test_invalid_pgbr_level_raises(minimal_project) -> None:
    cfg = load_project_config(minimal_project.repo_root)
    with pytest.raises(BackupLoggingConfigError, match="invalid pgBackRest log level"):
        merge_backup_logging_env(cfg, {"pgbackrest": {"log_level_console": "loud"}})


def test_invalid_restic_verbose_raises(minimal_project) -> None:
    cfg = load_project_config(minimal_project.repo_root)
    with pytest.raises(BackupLoggingConfigError, match="restic verbose"):
        merge_backup_logging_env(cfg, {"restic": {"verbose": 9}})
