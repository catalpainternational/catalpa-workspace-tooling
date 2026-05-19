"""Tests for catalpa_tooling.systemd_render."""

import pytest

from catalpa_tooling.config import SystemdUnitsOpsConfig
from catalpa_tooling.systemd_render import (
    KNOWN_UNIT_SUFFIXES,
    render_systemd_unit,
    systemd_templates_dir,
    template_suffix_for_unit,
    validate_systemd_units,
)


def test_systemd_templates_dir_contains_all_suffixes() -> None:
    src = systemd_templates_dir()
    assert src.is_dir()
    for suffix in KNOWN_UNIT_SUFFIXES:
        assert (src / suffix).is_file(), f"missing template {suffix}"


def test_template_suffix_for_unit_marktwain_names() -> None:
    assert (
        template_suffix_for_unit("marktwain-pgbackrest-backup-full.service")
        == "pgbackrest-backup-full.service"
    )
    assert (
        template_suffix_for_unit("marktwain-restic-files-backup.timer")
        == "restic-files-backup.timer"
    )


def test_template_suffix_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown systemd unit"):
        template_suffix_for_unit("marktwain-backup.service")


def test_render_marktwain_pgbackrest_service() -> None:
    body = render_systemd_unit(
        "marktwain-pgbackrest-backup-full.service",
        install_prefix="/opt/marktwain",
        config_dir="/etc/marktwain",
    )
    assert "EnvironmentFile=-/etc/marktwain/pgbackrest-backup.env" in body
    assert "ExecStart=/opt/marktwain/pgbackrest-backup.sh full" in body
    assert "@INSTALL_PREFIX@" not in body
    assert "@CONFIG_DIR@" not in body


def test_render_marktwain_pgbackrest_diff_service() -> None:
    body = render_systemd_unit(
        "marktwain-pgbackrest-backup-diff.service",
        install_prefix="/opt/marktwain",
        config_dir="/etc/marktwain",
    )
    assert "ExecStart=/opt/marktwain/pgbackrest-backup.sh diff" in body


def test_template_suffix_for_unit_diff() -> None:
    assert (
        template_suffix_for_unit("marktwain-pgbackrest-backup-diff.timer")
        == "pgbackrest-backup-diff.timer"
    )


def test_render_pgbackrest_diff_timer_calendar() -> None:
    body = render_systemd_unit(
        "app-pgbackrest-backup-diff.timer",
        install_prefix="/opt/app",
        config_dir="/etc/app",
    )
    assert "OnCalendar=Mon..Sat *-*-* 03:15:00" in body


def test_render_marktwain_restic_service() -> None:
    body = render_systemd_unit(
        "marktwain-restic-files-backup.service",
        install_prefix="/opt/marktwain",
        config_dir="/etc/marktwain",
    )
    assert "WorkingDirectory=/opt/marktwain" in body
    assert "EnvironmentFile=-/etc/marktwain/restic-files-backup.env" in body


def test_render_timer_has_no_path_placeholders() -> None:
    body = render_systemd_unit(
        "app-pgbackrest-backup-full.timer",
        install_prefix="/opt/app",
        config_dir="/etc/app",
    )
    assert "OnCalendar=" in body
    assert "/opt/app" not in body


def test_validate_systemd_units_prefix_mismatch() -> None:
    units = SystemdUnitsOpsConfig(
        pgbackrest=("wrong-pgbackrest-backup-full.service",),
        restic=(),
        timers_enable_pgbackrest=(),
        timers_enable_restic=(),
    )
    errors = validate_systemd_units(units, "app-")
    assert any("does not start with" in e for e in errors)


def test_validate_systemd_units_unknown_suffix() -> None:
    units = SystemdUnitsOpsConfig(
        pgbackrest=("app-unknown-backup.service",),
        restic=(),
        timers_enable_pgbackrest=(),
        timers_enable_restic=(),
    )
    errors = validate_systemd_units(units, "app-")
    assert any("Unknown systemd unit" in e for e in errors)
