"""`db restore` with no pgBackRest keys and no local dump."""

from __future__ import annotations

import pytest

from catalpa_tooling import db_restore


def test_dry_run_does_not_fetch(minimal_project, monkeypatch, capsys) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("dry run fetched")

    monkeypatch.setattr(db_restore, "run_fetch_all_dbs", boom)

    rc = db_restore.run_unified_db_restore(
        minimal_project, compose_file="compose.yml", env_add={}, env_name="local", dry_run=True
    )

    assert rc == 0
    assert "would fetch" in capsys.readouterr().err


def test_fetch_without_ssh_host_is_one_line_error(minimal_project, monkeypatch, capsys) -> None:
    def no_host(*args, **kwargs):
        raise ValueError("fetch db requires ssh_host on the database entry")

    monkeypatch.setattr(db_restore, "run_fetch_all_dbs", no_host)

    rc = db_restore.run_unified_db_restore(
        minimal_project, compose_file="compose.yml", env_add={}, env_name="local"
    )

    assert rc == 1
    assert "db restore: fetch db requires ssh_host" in capsys.readouterr().err


def test_metabase_fetch_without_ssh_host_is_one_line_error(minimal_project, monkeypatch, capsys) -> None:
    monkeypatch.setattr(db_restore, "configured_app_dump_exists", lambda config: True)
    monkeypatch.setattr(db_restore, "configured_metabase_dump_exists", lambda config: False)
    monkeypatch.setattr(type(minimal_project), "has_metabase_fetch", lambda self: True)

    def no_host(*args, **kwargs):
        raise ValueError("fetch db requires ssh_host on the database entry")

    monkeypatch.setattr(db_restore, "run_fetch_all_dbs", no_host)

    assert db_restore._ensure_local_dumps(minimal_project, auto_fetch=True) == 1
    assert "db restore: fetch db requires ssh_host" in capsys.readouterr().err
