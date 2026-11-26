"""Tests for ``dk <env> db restore`` archive handling and ``ensure_db_service_running``.

These cover the helpers shared by ``dk <env> db restore`` and ``dk transfer``. The
``db pgrestore`` subcommand, which read an archive from stdin, was removed in 1.5.0 (#42).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.helpers import spy_compose_backend

from catalpa_tooling.pgbackrest_db import (
    ensure_db_service_running,
    pg_dump_archive_error,
)


def test_pg_dump_archive_error_accepts_custom_format(tmp_path: Path) -> None:
    dump = tmp_path / "app.custom"
    dump.write_bytes(b"PGDMP" + b"\x00" * 100)
    assert pg_dump_archive_error(dump) is None


def test_pg_dump_archive_error_rejects_missing_and_plain(tmp_path: Path) -> None:
    plain = tmp_path / "plain.sql"
    plain.write_bytes(b"-- PostgreSQL database dump\n")
    assert "not a file" in (pg_dump_archive_error(tmp_path / "missing.custom") or "")
    assert "does not look like a pg_dump -Fc archive" in (pg_dump_archive_error(plain) or "")


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


def _b(config):
    from catalpa_tooling.stack_backend import ComposeBackend

    b = ComposeBackend.from_parts(config, "dev", "compose.yml", {})
    b.__dict__["env"] = {}
    return b


def test_db_restore_file_restores_that_archive_and_runs_hooks(minimal_project, monkeypatch) -> None:
    """``db restore --file`` is what replaced ``db pgrestore --file`` (#42)."""
    from catalpa_tooling.stack_commands import db_restore

    archive = Path(minimal_project.repo_root) / "one.custom"
    archive.write_bytes(b"PGDMP" + b"\x00" * 64)
    calls = spy_compose_backend(monkeypatch)

    with patch("catalpa_tooling.compose_backups.pgbackrest_restore") as offline:
        rc = db_restore(_b(minimal_project), archive=archive, yes=True)

    assert rc == 0
    assert calls == ["ensure", ("copy", archive), "drop_create", "pg_restore", "rm", "hooks:app"]
    # pgBackRest is never consulted, even if its credentials were complete.
    offline.assert_not_called()


def test_db_restore_file_dry_run_runs_nothing(minimal_project, monkeypatch, capsys) -> None:
    from catalpa_tooling.stack_commands import db_restore

    archive = Path(minimal_project.repo_root) / "one.custom"
    calls = spy_compose_backend(monkeypatch)
    assert db_restore(_b(minimal_project), archive=archive, dry_run=True) == 0
    assert calls == []
    assert str(archive) in capsys.readouterr().err


@pytest.mark.parametrize("dumps", [False, True])
def test_db_restore_from_a_dump_asks_first(minimal_project, monkeypatch, capsys, dumps: bool) -> None:
    """``--file`` and ``--dumps`` replace the database: without --yes they ask, and without a
    TTY to ask on they refuse before touching anything (#124: never read stdin as the archive)."""
    from catalpa_tooling.stack_commands import db_restore

    dump = minimal_project.fetch_db_dump_path
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_bytes(b"PGDMP" + b"\x00" * 200_000)
    calls = spy_compose_backend(monkeypatch)
    kw = {"dumps": True} if dumps else {"archive": dump}

    with (
        patch.object(sys.stdin, "isatty", return_value=False),
        patch.object(sys.stdin, "read", side_effect=AssertionError("stdin read")),
    ):
        assert db_restore(_b(minimal_project), **kw) == 1
        assert calls == [] and "Pass --yes" in capsys.readouterr().err
        assert db_restore(_b(minimal_project), yes=True, **kw) == 0
    assert ("copy", dump) in calls


@pytest.mark.parametrize("content", [None, b"-- PostgreSQL database dump\n"])
def test_db_restore_bad_file_refuses_before_drop(
    minimal_project, capsys, monkeypatch, content: bytes | None
) -> None:
    from catalpa_tooling.stack_commands import db_restore

    archive = Path(minimal_project.repo_root) / "bad.custom"
    if content is not None:
        archive.write_bytes(content)
    calls = spy_compose_backend(monkeypatch)

    rc = db_restore(_b(minimal_project), archive=archive, yes=True)

    assert rc == 1
    assert calls == []  # not even the db was started
    assert "refusing before dropping anything" in capsys.readouterr().err
