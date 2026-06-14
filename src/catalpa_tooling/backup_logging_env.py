"""Merge pgBackRest / restic logging defaults from ``tooling.yaml`` and ``info.yaml`` into deploy env."""

from __future__ import annotations

from typing import Any

from catalpa_tooling.backup_logging_levels import (
    BACKUP_LOGGING_ENV_KEYS,
    parse_pgbackrest_logging_from_mapping,
    parse_restic_logging_from_mapping,
)
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.env_yaml import _yaml_mapping_to_env

_DEFAULT_PGBR_RESTORE_CONSOLE = "detail"
_DEFAULT_RESTIC_RESTORE_VERBOSE = 1


def _info_env_logging(info: dict | None) -> dict[str, str]:
    """``info.yaml`` ``env:`` — ``PGBR_*`` / ``RESTIC_*`` names or short ``log_level_console`` / ``verbose`` keys."""
    if not isinstance(info, dict):
        return {}
    env_map = info.get("env")
    if not isinstance(env_map, dict):
        return {}
    upper = _yaml_mapping_to_env(env_map)
    out = {
        k: v
        for k, v in upper.items()
        if k in BACKUP_LOGGING_ENV_KEYS and (v or "").strip()
    }
    out.update(parse_pgbackrest_logging_from_mapping(env_map, field_prefix="info.env"))
    out.update(parse_restic_logging_from_mapping(env_map, field_prefix="info.env"))
    return out


def _info_section_logging(info: dict | None) -> dict[str, str]:
    if not isinstance(info, dict):
        return {}
    pgb = info.get("pgbackrest")
    rst = info.get("restic")
    out: dict[str, str] = {}
    if isinstance(pgb, dict):
        out.update(parse_pgbackrest_logging_from_mapping(pgb, field_prefix="info.pgbackrest"))
    if isinstance(rst, dict):
        out.update(parse_restic_logging_from_mapping(rst, field_prefix="info.restic"))
    return out


def _tooling_logging(config: ProjectConfig) -> dict[str, str]:
    pgb = config.ops.pgbackrest
    rst = config.ops.restic
    pgb_map: dict[str, Any] = {}
    if pgb.log_level_console is not None:
        pgb_map["log_level_console"] = pgb.log_level_console
    if pgb.log_level_stderr is not None:
        pgb_map["log_level_stderr"] = pgb.log_level_stderr
    if pgb.restore_log_level_console is not None:
        pgb_map["restore_log_level_console"] = pgb.restore_log_level_console
    rst_map: dict[str, Any] = {}
    if rst.verbose is not None:
        rst_map["verbose"] = rst.verbose
    if rst.restore_verbose is not None:
        rst_map["restore_verbose"] = rst.restore_verbose
    return {
        **parse_pgbackrest_logging_from_mapping(pgb_map, field_prefix="ops.pgbackrest"),
        **parse_restic_logging_from_mapping(rst_map, field_prefix="ops.restic"),
    }


def merge_backup_logging_env(
    config: ProjectConfig,
    info: dict | None,
) -> dict[str, str]:
    """Defaults from ``tooling.yaml``, overridden by ``info.yaml`` ``pgbackrest`` / ``restic`` / ``env:``.

    Restore-specific defaults when still unset after merge:
    - ``PGBR_RESTORE_LOG_LEVEL_CONSOLE`` ← ``restore_log_level_console`` / ``log_level_console`` / ``detail``
    - ``RESTIC_RESTORE_VERBOSE`` ← ``restore_verbose`` / ``verbose`` / ``1``
    """
    merged: dict[str, str] = {}
    merged.update(_tooling_logging(config))
    merged.update(_info_section_logging(info))
    merged.update(_info_env_logging(info))

    console = (merged.get("PGBR_LOG_LEVEL_CONSOLE") or "").strip()
    if not (merged.get("PGBR_RESTORE_LOG_LEVEL_CONSOLE") or "").strip():
        merged["PGBR_RESTORE_LOG_LEVEL_CONSOLE"] = console or _DEFAULT_PGBR_RESTORE_CONSOLE

    verbose = (merged.get("RESTIC_VERBOSE") or "").strip()
    if not (merged.get("RESTIC_RESTORE_VERBOSE") or "").strip():
        merged["RESTIC_RESTORE_VERBOSE"] = verbose or str(_DEFAULT_RESTIC_RESTORE_VERBOSE)

    return {k: v for k, v in merged.items() if k in BACKUP_LOGGING_ENV_KEYS and v != ""}


def apply_backup_logging_env(
    env_add: dict[str, str],
    config: ProjectConfig,
    info: dict | None,
) -> None:
    """Merge logging env into ``env_add`` (overrides credential keys for these names)."""
    env_add.update(merge_backup_logging_env(config, info))
