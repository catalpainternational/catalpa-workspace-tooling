"""Tests for compose ``pg_restore --role`` handling."""

from catalpa_tooling.pgbackrest_db import (
    _pg_restore_compose_role_suffix,
    _pg_restore_has_role,
    _pg_restore_owner_acl_extras,
)


def test_pg_restore_owner_acl_extras_adds_no_owner_and_no_acl() -> None:
    assert _pg_restore_owner_acl_extras([]) == ["--no-acl", "--no-owner"]


def test_pg_restore_has_role_detects_flag_and_equals_form() -> None:
    assert not _pg_restore_has_role(["--no-owner"])
    assert _pg_restore_has_role(["--role", "bero"])
    assert _pg_restore_has_role(["--role=bero"])


def test_pg_restore_compose_role_suffix_defaults_to_app_user() -> None:
    assert _pg_restore_compose_role_suffix(["--no-owner"]) == ' --role "$APP_USER"'


def test_pg_restore_compose_role_suffix_skips_when_role_provided() -> None:
    assert _pg_restore_compose_role_suffix(["--role", "other"]) == ""
    assert _pg_restore_compose_role_suffix(["--role=other"]) == ""
