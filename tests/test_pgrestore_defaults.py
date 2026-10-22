"""Tests for ``dk <env> db restore`` archive handling and ``ensure_db_service_running``.

These cover the helpers shared by ``dk <env> db restore`` and ``dk transfer``. The
``db pgrestore`` subcommand, which read an archive from stdin, was removed in 1.5.0 (#42).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

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


def test_db_restore_file_restores_that_archive_and_runs_hooks(minimal_project) -> None:
    """``db restore --file`` is what replaced ``db pgrestore --file`` (#42)."""
    from catalpa_tooling.db_restore import run_unified_db_restore

    archive = Path(minimal_project.repo_root) / "one.custom"
    archive.write_bytes(b"PGDMP" + b"\x00" * 64)
    order: list[str] = []

    with (
        patch(
            "catalpa_tooling.db_restore.ensure_db_service_running",
            return_value=0,
        ),
        patch(
            "catalpa_tooling.db_restore.run_drop_create_app_database",
            side_effect=lambda *a, **k: (order.append("drop_create") or 0),
        ),
        patch(
            "catalpa_tooling.db_restore.run_pg_restore",
            side_effect=lambda *a, **k: (order.append("pg_restore") or 0),
        ) as pg_restore,
        patch(
            "catalpa_tooling.db_restore.run_post_db_restore_manage_commands",
            side_effect=lambda *a, **k: (order.append("hooks") or 0),
        ),
        patch("catalpa_tooling.db_restore.run_restore_offline") as offline,
    ):
        rc = run_unified_db_restore(
            minimal_project,
            compose_file="compose.yml",
            env_add={},
            env_name="dev",
            archive_path=archive,
        )

    assert rc == 0
    assert order == ["drop_create", "pg_restore", "hooks"]
    # pgBackRest is never consulted, even if its credentials were complete.
    offline.assert_not_called()
    extras = pg_restore.call_args.args[2]
    assert "--file" in extras
    assert extras[extras.index("--file") + 1] == str(archive)


def test_db_restore_file_dry_run_runs_nothing(minimal_project, capsys) -> None:
    from catalpa_tooling.db_restore import run_unified_db_restore

    archive = Path(minimal_project.repo_root) / "one.custom"
    with patch("catalpa_tooling.db_restore.run_compose_app_dump_restore") as restore:
        rc = run_unified_db_restore(
            minimal_project,
            compose_file="compose.yml",
            env_add={},
            env_name="dev",
            archive_path=archive,
            dry_run=True,
        )
    assert rc == 0
    restore.assert_not_called()
    assert str(archive) in capsys.readouterr().err


def test_db_restore_dumps_without_tty_uses_configured_dump(minimal_project) -> None:
    """#124: ``--dumps`` from a non-interactive shell must not read the archive from stdin."""
    from catalpa_tooling.db_restore import run_unified_db_restore

    dump = minimal_project.fetch_db_dump_path
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_bytes(b"PGDMP" + b"\x00" * 200_000)

    with (
        patch.object(sys.stdin, "isatty", return_value=False),
        patch.object(sys.stdin, "read", side_effect=AssertionError("stdin read")),
        patch("catalpa_tooling.db_restore.ensure_db_service_running", return_value=0),
        patch("catalpa_tooling.db_restore.run_drop_create_app_database", return_value=0),
        patch("catalpa_tooling.db_restore.run_pg_restore", return_value=0) as pg_restore,
        patch("catalpa_tooling.db_restore.run_post_db_restore_manage_commands", return_value=0),
    ):
        rc = run_unified_db_restore(
            minimal_project,
            compose_file="compose.yml",
            env_add={},
            env_name="dev",
            force_dumps=True,
        )

    assert rc == 0
    extras = pg_restore.call_args.args[2]
    assert extras[extras.index("--file") + 1] == str(dump)


@pytest.mark.parametrize("content", [None, b"-- PostgreSQL database dump\n"])
def test_db_restore_bad_file_refuses_before_drop(
    minimal_project, capsys, content: bytes | None
) -> None:
    from catalpa_tooling.db_restore import run_unified_db_restore

    archive = Path(minimal_project.repo_root) / "bad.custom"
    if content is not None:
        archive.write_bytes(content)

    with (
        patch("catalpa_tooling.db_restore.ensure_db_service_running") as ensure,
        patch("catalpa_tooling.db_restore.run_drop_create_app_database") as drop,
        patch("catalpa_tooling.db_restore.run_pg_restore") as pg_restore,
    ):
        rc = run_unified_db_restore(
            minimal_project,
            compose_file="compose.yml",
            env_add={},
            env_name="dev",
            archive_path=archive,
        )

    assert rc == 1
    ensure.assert_not_called()
    drop.assert_not_called()
    pg_restore.assert_not_called()
    assert "refusing before dropping anything" in capsys.readouterr().err
