"""Tests for ``bkp_db pgrestore`` default archive and ``ensure_db_service_running``."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from catalpa_tooling.pgbackrest_db import (
    ensure_db_service_running,
    pg_restore_extras_with_default_archive,
)


def test_pg_restore_extras_uses_default_when_tty(tmp_path: Path) -> None:
    dump = tmp_path / "app.custom"
    dump.write_bytes(b"PGDMP" + b"\x00" * 100)

    with patch.object(sys.stdin, "isatty", return_value=True):
        out = pg_restore_extras_with_default_archive([], dump)

    assert out[:2] == ["--file", str(dump.resolve())]


def test_pg_restore_extras_keeps_explicit_file(tmp_path: Path) -> None:
    explicit = tmp_path / "other.custom"
    explicit.write_bytes(b"x")
    default = tmp_path / "default.custom"
    default.write_bytes(b"PGDMP")

    with patch.object(sys.stdin, "isatty", return_value=True):
        out = pg_restore_extras_with_default_archive(
            ["--file", str(explicit), "--jobs", "4"],
            default,
        )

    assert out == ["--file", str(explicit), "--jobs", "4"]


def test_pg_restore_extras_no_default_when_stdin_not_tty(tmp_path: Path) -> None:
    dump = tmp_path / "default.custom"
    dump.write_bytes(b"PGDMP")

    with patch.object(sys.stdin, "isatty", return_value=False):
        out = pg_restore_extras_with_default_archive([], dump)

    assert out == []


def test_pg_restore_extras_missing_default_prints_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.custom"

    with patch.object(sys.stdin, "isatty", return_value=True):
        out = pg_restore_extras_with_default_archive([], missing)

    assert out == []
    assert "no dump at" in capsys.readouterr().err


def test_ensure_db_skips_up_when_already_running() -> None:
    with (
        patch(
            "catalpa_tooling.pgbackrest_db.db_service_responds",
            return_value=True,
        ) as responds,
        patch("catalpa_tooling.pgbackrest_db.ensure_db_compose_volumes") as ensure_volumes,
        patch("catalpa_tooling.pgbackrest_db.run_cmd") as run_cmd,
    ):
        assert ensure_db_service_running("compose.yml", {}) == 0
    responds.assert_called_once()
    ensure_volumes.assert_not_called()
    run_cmd.assert_not_called()


def test_ensure_db_ensures_volumes_before_up() -> None:
    with (
        patch(
            "catalpa_tooling.pgbackrest_db.db_service_responds",
            side_effect=[False, True],
        ),
        patch(
            "catalpa_tooling.pgbackrest_db.ensure_db_compose_volumes",
            return_value=0,
        ) as ensure_volumes,
        patch("catalpa_tooling.pgbackrest_db.run_cmd") as run_cmd,
    ):
        run_cmd.return_value = type("R", (), {"returncode": 0})()
        assert ensure_db_service_running("compose.yml", {}) == 0
    ensure_volumes.assert_called_once_with({}, config=None)


def test_ensure_db_starts_service_when_down() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> object:
        calls.append(list(cmd))
        m = type("R", (), {"returncode": 0})()
        return m

    with (
        patch(
            "catalpa_tooling.pgbackrest_db.db_service_responds",
            side_effect=[False, True],
        ),
        patch(
            "catalpa_tooling.pgbackrest_db.ensure_db_compose_volumes",
            return_value=0,
        ),
        patch("catalpa_tooling.pgbackrest_db.run_cmd", side_effect=fake_run),
    ):
        assert ensure_db_service_running("compose.yml", {}) == 0

    assert calls[0][4:8] == ["up", "-d", "db", "--wait"]
