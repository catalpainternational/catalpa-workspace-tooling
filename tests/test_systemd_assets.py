"""Tests for bundled systemd package data."""

from catalpa_tooling.systemd_assets import systemd_source_dir
from catalpa_tooling.systemd_render import KNOWN_UNIT_SUFFIXES, systemd_templates_dir


def test_systemd_source_dir_contains_scripts() -> None:
    src = systemd_source_dir()
    assert src.is_dir()
    assert (src / "pgbackrest-backup.sh").is_file()
    assert (src / "restic-files-backup.sh").is_file()


def test_systemd_templates_dir_contains_canonical_units() -> None:
    templates = systemd_templates_dir()
    assert templates.is_dir()
    for suffix in KNOWN_UNIT_SUFFIXES:
        assert (templates / suffix).is_file(), f"missing template {suffix}"
