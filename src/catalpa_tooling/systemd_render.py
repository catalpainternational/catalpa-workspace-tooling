"""Render project-specific systemd unit files from bundled templates."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from catalpa_tooling.config import SystemdUnitsOpsConfig

# Longest suffix first so e.g. pgbackrest-backup-full.service wins over shorter matches.
KNOWN_UNIT_SUFFIXES: tuple[str, ...] = (
    "pgbackrest-backup-full.service",
    "pgbackrest-backup-incr.service",
    "pgbackrest-backup-diff.service",
    "pgbackrest-backup-full.timer",
    "pgbackrest-backup-incr.timer",
    "pgbackrest-backup-diff.timer",
    "restic-files-backup.service",
    "restic-files-backup.timer",
)

_SERVICE_SUFFIXES = frozenset(
    s for s in KNOWN_UNIT_SUFFIXES if s.endswith(".service")
)


def systemd_templates_dir() -> Path:
    """Directory containing canonical ``.service`` / ``.timer`` templates."""
    return Path(files("catalpa_tooling").joinpath("systemd", "templates"))


def template_suffix_for_unit(unit_name: str) -> str:
    """Return the known template suffix for ``unit_name`` (e.g. ``pgbackrest-backup-full.service``)."""
    name = (unit_name or "").strip()
    if not name:
        raise ValueError("systemd unit name is empty")
    for suffix in KNOWN_UNIT_SUFFIXES:
        if name.endswith(suffix):
            return suffix
    known = ", ".join(KNOWN_UNIT_SUFFIXES)
    raise ValueError(
        f"Unknown systemd unit {name!r}; name must end with one of: {known}"
    )


def render_systemd_unit(
    unit_name: str,
    *,
    install_prefix: str,
    config_dir: str,
) -> str:
    """Render a unit or timer file for installation under ``/etc/systemd/system/``."""
    suffix = template_suffix_for_unit(unit_name)
    template_path = systemd_templates_dir() / suffix
    if not template_path.is_file():
        raise FileNotFoundError(f"Missing systemd template: {template_path}")
    body = template_path.read_text(encoding="utf-8")
    if suffix in _SERVICE_SUFFIXES:
        prefix = (install_prefix or "").strip().rstrip("/")
        cfg = (config_dir or "").strip().rstrip("/")
        if not prefix:
            raise ValueError("ops.install_prefix is empty")
        if not cfg:
            raise ValueError("ops.config_dir is empty")
        body = body.replace("@INSTALL_PREFIX@", prefix).replace("@CONFIG_DIR@", cfg)
    return body


def validate_systemd_units(
    units: SystemdUnitsOpsConfig,
    unit_prefix: str,
) -> list[str]:
    """Return human-readable errors for invalid ``ops.systemd_units`` entries."""
    prefix = (unit_prefix or "").strip()
    errors: list[str] = []
    all_names: list[tuple[str, str]] = [
        ("pgbackrest", n) for n in units.pgbackrest
    ] + [("restic", n) for n in units.restic] + [
        ("timers_enable_pgbackrest", n) for n in units.timers_enable_pgbackrest
    ] + [("timers_enable_restic", n) for n in units.timers_enable_restic]

    for section, name in all_names:
        if prefix and not name.startswith(prefix):
            errors.append(
                f"ops.systemd_units.{section}: {name!r} does not start with "
                f"ops.systemd_unit_prefix {prefix!r}"
            )
        try:
            template_suffix_for_unit(name)
        except ValueError as e:
            errors.append(f"ops.systemd_units.{section}: {e}")
    return errors
