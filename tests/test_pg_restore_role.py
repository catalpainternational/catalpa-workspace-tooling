"""Tests for compose ``pg_restore --role`` handling."""

from pathlib import Path

from catalpa_tooling.config import load_project_config
from catalpa_tooling.pgbackrest_db import (
    _pg_restore_compose_role_suffix,
    _pg_restore_has_role,
    _pg_restore_owner_acl_extras,
    compose_pg_restore_extras_for_config,
    pg_restore_compose_extras,
)
from tests.test_fetch_config import _write_minimal_tooling


def test_compose_pg_restore_extras_merges_tooling_yaml_pg_restore_args(
    tmp_path: Path,
    isolated_tooling: None,
) -> None:
    _write_minimal_tooling(
        tmp_path,
        extra="""
native:
  reset_db:
    postgis: true
    pg_restore_args:
      - --role=postgres
""",
    )
    cfg = load_project_config(tmp_path)
    assert compose_pg_restore_extras_for_config(cfg, []) == [
        "--no-acl",
        "--no-owner",
        "--role=postgres",
    ]


def test_pg_restore_owner_acl_extras_adds_no_owner_and_no_acl() -> None:
    assert _pg_restore_owner_acl_extras([]) == ["--no-acl", "--no-owner"]


def test_pg_restore_has_role_detects_flag_and_equals_form() -> None:
    assert not _pg_restore_has_role(["--no-owner"])
    assert _pg_restore_has_role(["--role", "bero"])
    assert _pg_restore_has_role(["--role=bero"])


def test_pg_restore_compose_role_suffix_defaults_to_app_user() -> None:
    assert _pg_restore_compose_role_suffix(["--no-owner"]) == ' --role "$APP_USER"'


def test_pg_restore_compose_role_suffix_defaults_to_postgres_for_postgis() -> None:
    assert _pg_restore_compose_role_suffix(["--no-owner"], postgis=True) == " --role postgres"


def test_pg_restore_compose_role_suffix_skips_when_role_provided() -> None:
    assert _pg_restore_compose_role_suffix(["--role", "other"]) == ""
    assert _pg_restore_compose_role_suffix(["--role=other"]) == ""


def test_pg_restore_compose_extras_adds_no_owner_and_no_acl() -> None:
    assert pg_restore_compose_extras([]) == ["--no-acl", "--no-owner"]
