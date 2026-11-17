"""Tests for compose ``pg_restore --role`` handling."""

import os
import subprocess
from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.pgbackrest_db import (
    _pg_restore_compose_inner_script,
    _pg_restore_compose_role_suffix,
    _pg_restore_has_role,
    _pg_restore_owner_acl_extras,
    _pg_restore_promote_app_superuser,
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
    assert (
        _pg_restore_compose_role_suffix(["--no-owner"], postgis=True)
        == " --role postgres"
    )


def test_pg_restore_compose_role_suffix_prefers_app_user_when_restore_as_super() -> None:
    assert (
        _pg_restore_compose_role_suffix(
            ["--no-owner"], postgis=True, restore_as_super=True
        )
        == ' --role "$APP_USER"'
    )


def test_pg_restore_compose_role_suffix_skips_when_role_provided() -> None:
    assert _pg_restore_compose_role_suffix(["--role", "other"]) == ""
    assert _pg_restore_compose_role_suffix(["--role=other"]) == ""


def test_pg_restore_compose_extras_adds_no_owner_and_no_acl() -> None:
    assert pg_restore_compose_extras([]) == ["--no-acl", "--no-owner"]


def test_pg_restore_promote_app_superuser_off_by_default() -> None:
    assert _pg_restore_promote_app_superuser(["--no-owner"], target="app") is False


def test_pg_restore_promote_app_superuser_when_enabled() -> None:
    assert _pg_restore_promote_app_superuser(
        ["--no-owner"], target="app", restore_as_super=True
    ) is True


def test_pg_restore_promote_app_superuser_skips_explicit_postgres_role() -> None:
    assert _pg_restore_promote_app_superuser(
        ["--role=postgres"], target="app", restore_as_super=True
    ) is False


def test_pg_restore_promote_app_superuser_skips_metabase_target() -> None:
    assert _pg_restore_promote_app_superuser(
        [], target="metabase", restore_as_super=True
    ) is False


def test_pg_restore_inner_script_promotes_app_user() -> None:
    script = _pg_restore_compose_inner_script(
        "app",
        ["--no-acl", "--no-owner"],
        container_path="/tmp/test.dump",
        promote_app_superuser=True,
        postgis=True,
        restore_as_super=True,
    )
    assert "ALTER ROLE" in script and "SUPERUSER" in script
    assert "NOSUPERUSER" in script
    assert 'trap demote_app_user EXIT' in script
    assert '--role "$APP_USER"' in script
    assert "/tmp/test.dump" in script
    assert "exec pg_restore" not in script


def test_pg_restore_inner_script_uses_postgres_role_for_postgis_only() -> None:
    script = _pg_restore_compose_inner_script(
        "app",
        ["--no-acl", "--no-owner"],
        container_path="/tmp/test.dump",
        promote_app_superuser=False,
        postgis=True,
        restore_as_super=False,
    )
    assert "SUPERUSER" not in script
    assert "--role postgres" in script


def test_pg_restore_inner_script_skips_promotion_when_disabled() -> None:
    script = _pg_restore_compose_inner_script(
        "app",
        ["--role=postgres"],
        container_path="/tmp/test.dump",
        promote_app_superuser=False,
        postgis=False,
        restore_as_super=False,
    )
    assert "SUPERUSER" not in script
    assert "pg_restore -h" in script


# --- issue #163: a PostGIS restore as postgres must still leave the app role owning its objects --


def _postgis_script(extras: list[str] | None = None, **kwargs) -> str:
    opts = {"promote_app_superuser": False, "postgis": True, "restore_as_super": False}
    opts.update(kwargs)
    target = opts.pop("target", "app")
    return _pg_restore_compose_inner_script(
        target,
        ["--no-acl", "--no-owner"] if extras is None else extras,
        container_path="/tmp/test.dump",
        **opts,
    )


def test_postgis_restore_hands_restored_objects_to_the_app_role() -> None:
    """#163: restored as postgres, the tables were postgres's, so `ALTER TABLE` migrations failed."""
    script = _postgis_script()
    assert "--role postgres" in script, "PostGIS still needs the superuser restore"
    restore_at = script.index("pg_restore -h")
    reassign_at = script.index("OWNER TO")
    assert restore_at < reassign_at, "ownership must move after the restore, not before"
    assert '-v app_user="$APP_USER"' in script
    for kind in ("TABLE", "VIEW", "MATERIALIZED VIEW", "SEQUENCE", "FUNCTION", "DOMAIN", "SCHEMA"):
        assert kind in script
    # PostGIS's own objects stay with postgres, as on a database the app migrated itself.
    assert "deptype = 'e'" in script


def test_reassign_is_left_out_when_the_restore_already_runs_as_the_app_role() -> None:
    assert "OWNER TO" not in _postgis_script(postgis=False)
    assert "OWNER TO" not in _postgis_script(
        promote_app_superuser=True, restore_as_super=True
    )
    assert "OWNER TO" not in _postgis_script(target="metabase", postgis=False)


def test_reassign_respects_an_explicit_role_in_pg_restore_args() -> None:
    """An explicit `--role` is the project's own choice; don't second-guess it."""
    assert "OWNER TO" not in _postgis_script(["--no-acl", "--no-owner", "--role=postgres"])


@pytest.mark.parametrize(
    ("restore_rc", "psql_rc", "expected"),
    [(0, 0, 0), (0, 3, 3), (1, 0, 1), (1, 3, 1)],
)
def test_reassign_runs_even_after_pg_restore_errors_and_its_status_wins(
    tmp_path: Path, restore_rc: int, psql_rc: int, expected: int
) -> None:
    """pg_restore exits 1 for ignored errors yet restores nearly everything: fix owners anyway."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    for name, rc in (("pg_restore", restore_rc), ("psql", psql_rc)):
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\ncat >/dev/null\necho {name} >> {log}\nexit {rc}\n")
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "POSTGRES_PASSWORD": "x"}
    result = subprocess.run(
        ["sh", "-c", _postgis_script()], env=env, stdin=subprocess.DEVNULL, check=False
    )
    assert log.read_text().split() == ["pg_restore", "psql"]
    assert result.returncode == expected
