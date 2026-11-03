"""Database commands run as `postgres` over the db container's socket, never with a password
over TCP: a cluster restored from another host keeps that host's postgres password."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from catalpa_tooling import pgbackrest_db


def _exec_user(argv: list[str]) -> list[str]:
    return argv[argv.index("exec") : argv.index("db")]


def _assert_socket(argv: list[str]) -> None:
    assert _exec_user(argv) == ["exec", "-T", "-u", "postgres"]
    script = argv[-1]
    assert "PGPASSWORD" not in script
    assert "-h " not in script


@pytest.fixture
def calls():
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return MagicMock(returncode=0, stderr=b"", stdout="")

    with patch("catalpa_tooling.pgbackrest_db.run_cmd", side_effect=fake_run):
        yield seen


def test_pg_dump(calls) -> None:
    assert pgbackrest_db.run_pg_dump("compose.yml", {}) == 0
    _assert_socket(calls[0])


def test_pg_dump_to_file(calls, tmp_path: Path) -> None:
    pgbackrest_db.run_pg_dump_to_file("compose.yml", {}, tmp_path / "app.dump")
    _assert_socket(calls[0])


def test_pg_restore(calls, tmp_path: Path) -> None:
    archive = tmp_path / "app.dump"
    archive.write_bytes(b"PGDMP" + b"\0" * 2048)

    pgbackrest_db.run_pg_restore("compose.yml", {}, ["--file", str(archive)])

    restores = [c for c in calls if "pg_restore" in c[-1]]
    assert restores, calls
    _assert_socket(restores[0])


def test_promote_script_uses_the_socket() -> None:
    script = pgbackrest_db._pg_restore_compose_inner_script(
        "app", [], container_path="/tmp/a.dump", promote_app_superuser=True
    )
    assert "SUPERUSER" in script
    assert "-h " not in script
    assert "PGPASSWORD" not in script
