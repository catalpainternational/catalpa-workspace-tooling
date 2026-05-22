"""Tests for ``dev reset-db`` flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.dev_cli import (
    _MIN_CUSTOM_DUMP_BYTES,
    _pg_env_for_cli,
    _resolve_reset_dump_path,
    _run_reset_db_drop_create_migrate_seed,
)
from tests.test_fetch_media_config import _write_minimal_tooling


def test_pg_env_uses_configured_keys(tmp_path: Path, isolated_tooling: None, monkeypatch) -> None:
    _write_minimal_tooling(tmp_path)
    (tmp_path / "tooling.yaml").write_text(
        (tmp_path / "tooling.yaml").read_text(encoding="utf-8")
        + """
dev:
  reset_db:
    db_name_env: [DJANGO_DB]
    db_name_fallback: catalpa_db
    host_env: [DATABASE_HOST]
""",
        encoding="utf-8",
    )
    (tmp_path / ".env.local").write_text(
        "DJANGO_DB=mydb\nDATABASE_HOST=dbhost\n",
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    dbname, env = _pg_env_for_cli(cfg)
    assert dbname == "mydb"
    assert env["PGHOST"] == "dbhost"


def test_pg_env_defaults_from_project_name(
    tmp_path: Path, isolated_tooling: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_minimal_tooling(tmp_path)
    monkeypatch.chdir(tmp_path)
    for key in (
        "DJANGO_DB",
        "DJANGO_DB_NAME",
        "POSTGRES_DB",
        "DJANGO_DB_HOST",
        "POSTGRES_HOST",
        "DATABASE_HOST",
        "DJANGO_DB_PORT",
        "POSTGRES_PORT",
        "DJANGO_DB_USER",
        "POSTGRES_USER",
        "PGUSER",
    ):
        monkeypatch.delenv(key, raising=False)
    cfg = load_project_config(tmp_path)
    dbname, env = _pg_env_for_cli(cfg)
    assert dbname == "test_db"
    assert env["PGHOST"] == "localhost"
    assert env["PGPORT"] == "5432"
    assert "PGUSER" not in env


def test_resolve_dump_auto_from_fetch_db_dump(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    dump = cfg.fetch_db_dump_path
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_bytes(b"pg custom format")
    assert _resolve_reset_dump_path(cfg, None, explicit=False) == dump.resolve()


def test_resolve_dump_missing_without_explicit(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    assert _resolve_reset_dump_path(cfg, None, explicit=False) is None


def test_reset_db_uses_dump_branch(tmp_path: Path, isolated_tooling: None, monkeypatch) -> None:
    _write_minimal_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    dump = cfg.fetch_db_dump_path
    dump.parent.mkdir(parents=True, exist_ok=True)
    dump.write_bytes(b"x" * _MIN_CUSTOM_DUMP_BYTES)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/true")
    monkeypatch.setattr(
        "catalpa_tooling.dev_cli.run_cmd",
        MagicMock(return_value=MagicMock(returncode=0)),
    )
    monkeypatch.setattr("catalpa_tooling.dev_cli._require_usable_custom_dump", lambda _p: None)
    monkeypatch.setattr("catalpa_tooling.dev_cli._public_table_count", lambda *_a, **_k: 1)
    monkeypatch.setattr("catalpa_tooling.dev_cli._run_pg_restore", lambda *_a, **_k: 0)
    monkeypatch.setattr("catalpa_tooling.dev_cli.run_reset_db_post_manage_commands", lambda _c: 0)

    rc = _run_reset_db_drop_create_migrate_seed(from_dump=None, explicit_dump=False)
    assert rc == 0


def test_reset_db_migrate_without_postgis(tmp_path: Path, isolated_tooling: None, monkeypatch) -> None:
    _write_minimal_tooling(tmp_path)
    load_project_config(tmp_path)
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **kwargs):
        calls.append(list(cmd))
        return MagicMock(returncode=0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/true")
    monkeypatch.setattr("catalpa_tooling.dev_cli.run_cmd", fake_run_cmd)
    monkeypatch.setattr("catalpa_tooling.dev_cli._run_uv_manage", lambda _a: 0)
    monkeypatch.setattr("catalpa_tooling.dev_cli.run_reset_db_post_manage_commands", lambda _c: 0)
    monkeypatch.setattr("catalpa_tooling.dev_cli.reset_db_post_script", lambda _d: None)

    rc = _run_reset_db_drop_create_migrate_seed(from_dump=None, explicit_dump=False)
    assert rc == 0
    psql_calls = [c for c in calls if c and c[0] == "psql"]
    assert not psql_calls
