"""Tests for Django manage env alignment with ``dev reset-db``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from catalpa_tooling.config import DEFAULT_LOCAL_PG_HOST, DEFAULT_LOCAL_PG_PORT, load_project_config
from catalpa_tooling.dev_cli import _django_manage_dev_env, _pg_env_for_cli
from tests.test_fetch_media_config import _write_minimal_tooling


def test_manage_env_sets_database_host_like_reset_db(
    tmp_path: Path, isolated_tooling: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_minimal_tooling(tmp_path)
    (tmp_path / "tooling.yaml").write_text(
        (tmp_path / "tooling.yaml").read_text(encoding="utf-8").replace(
            "fetch_db_dump: dump", "fetch_db_dump: dumps/catalpa_db.custom"
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    for key in (
        "DJANGO_DB",
        "POSTGRES_DB",
        "DATABASE_HOST",
        "POSTGRES_HOST",
        "DATABASE_PORT",
        "POSTGRES_PORT",
        "DJANGO_DB_HOST",
        "DJANGO_DB_PORT",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = load_project_config(tmp_path)
    _, pg_env = _pg_env_for_cli(cfg)
    manage_env = _django_manage_dev_env(cfg)

    assert pg_env["PGHOST"] == DEFAULT_LOCAL_PG_HOST
    assert manage_env["DATABASE_HOST"] == DEFAULT_LOCAL_PG_HOST
    assert manage_env["POSTGRES_HOST"] == DEFAULT_LOCAL_PG_HOST
    assert manage_env["DATABASE_PORT"] == DEFAULT_LOCAL_PG_PORT
    assert manage_env["DJANGO_DB"] == "catalpa_db"
    assert manage_env["DJANGO_DB_HOST"] == DEFAULT_LOCAL_PG_HOST

    merged = os.environ.copy()
    merged.update(manage_env)
    assert merged.get("DJANGO_DB_HOST") == pg_env["PGHOST"]
    assert merged.get("DJANGO_DB_PORT") == pg_env["PGPORT"]
    assert merged.get("DJANGO_DB") == "catalpa_db"
