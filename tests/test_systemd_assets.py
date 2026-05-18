"""Tests for bundled systemd package data."""

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.systemd_assets import systemd_source_dir


def test_systemd_source_dir_contains_scripts() -> None:
    src = systemd_source_dir()
    assert src.is_dir()
    assert (src / "pgbackrest-backup.sh").is_file()
    assert (src / "restic-files-backup.sh").is_file()


def test_systemd_source_dir_contains_minimal_fixture_units(minimal_project: ProjectConfig) -> None:
    config = minimal_project
    src = systemd_source_dir()
    for name in config.ops.systemd_units.pgbackrest:
        assert (src / name).is_file(), f"missing {name}"
    for name in config.ops.systemd_units.restic:
        assert (src / name).is_file(), f"missing {name}"
