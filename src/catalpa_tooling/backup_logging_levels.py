"""Parse pgBackRest / restic logging settings from YAML mappings (no ``ProjectConfig`` import)."""

from __future__ import annotations

from typing import Any

BACKUP_LOGGING_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PGBR_LOG_LEVEL_CONSOLE",
        "PGBR_LOG_LEVEL_STDERR",
        "PGBR_RESTORE_LOG_LEVEL_CONSOLE",
        "RESTIC_VERBOSE",
        "RESTIC_RESTORE_VERBOSE",
    }
)

_PGBR_LOG_LEVELS: frozenset[str] = frozenset(
    {"off", "error", "warn", "info", "detail", "debug", "trace"}
)

_PGBR_SECTION_KEYS: tuple[tuple[str, str], ...] = (
    ("log_level_console", "PGBR_LOG_LEVEL_CONSOLE"),
    ("log_level_stderr", "PGBR_LOG_LEVEL_STDERR"),
    ("restore_log_level_console", "PGBR_RESTORE_LOG_LEVEL_CONSOLE"),
)
_RESTIC_SECTION_KEYS: tuple[tuple[str, str], ...] = (
    ("verbose", "RESTIC_VERBOSE"),
    ("restore_verbose", "RESTIC_RESTORE_VERBOSE"),
)


class BackupLoggingConfigError(ValueError):
    """Invalid logging value in ``tooling.yaml`` or ``info.yaml``."""


def parse_optional_pgbr_log_level(raw: Any, *, field: str) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    v = s.lower()
    if v not in _PGBR_LOG_LEVELS:
        raise BackupLoggingConfigError(
            f"{field}: invalid pgBackRest log level {raw!r} "
            f"(expected one of: {', '.join(sorted(_PGBR_LOG_LEVELS))})"
        )
    return v


def parse_optional_restic_verbose(raw: Any, *, field: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1 if raw else 0
    if isinstance(raw, int):
        n = raw
    else:
        s = str(raw).strip().lower()
        if not s:
            return None
        if s in ("true", "yes", "on"):
            return 1
        if s in ("false", "no", "off"):
            return 0
        if not s.isdigit():
            raise BackupLoggingConfigError(
                f"{field}: restic verbose must be 0–4 or true/false, got {raw!r}"
            )
        n = int(s)
    if n < 0 or n > 4:
        raise BackupLoggingConfigError(f"{field}: restic verbose must be 0–4, got {n}")
    return n


def parse_pgbackrest_logging_from_mapping(
    data: dict[str, Any] | None,
    *,
    field_prefix: str,
) -> dict[str, str]:
    if not data:
        return {}
    out: dict[str, str] = {}
    for yaml_key, env_key in _PGBR_SECTION_KEYS:
        if yaml_key not in data:
            continue
        level = parse_optional_pgbr_log_level(
            data.get(yaml_key),
            field=f"{field_prefix}.{yaml_key}",
        )
        if level is not None:
            out[env_key] = level
    return out


def parse_restic_logging_from_mapping(
    data: dict[str, Any] | None,
    *,
    field_prefix: str,
) -> dict[str, str]:
    if not data:
        return {}
    out: dict[str, str] = {}
    for yaml_key, env_key in _RESTIC_SECTION_KEYS:
        if yaml_key not in data:
            continue
        n = parse_optional_restic_verbose(
            data.get(yaml_key),
            field=f"{field_prefix}.{yaml_key}",
        )
        if n is not None:
            out[env_key] = str(n)
    return out
