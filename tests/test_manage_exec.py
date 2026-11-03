"""Tests for ``stack.manage_user`` and ``dk <env> manage --user`` (#86)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from catalpa_tooling.cli.dk_argv import normalize_dk_env_argv
from catalpa_tooling.compose import manage_exec_args
from catalpa_tooling.config import PostDbRestoreOpsConfig, ProjectConfigError, load_project_config
from catalpa_tooling.dk_parser import build_dk_parser
from catalpa_tooling.post_db_restore import run_post_db_restore_manage_commands
from tests.helpers import write_minimal_tooling_tree


def _with_manage_user(cfg, user: str | None):
    return replace(cfg, stack_optional=replace(cfg.stack, manage_user=user))


def _write_stack_manage_user(tmp_path: Path, value: str) -> None:
    write_minimal_tooling_tree(tmp_path)
    tooling = tmp_path / "tooling.yaml"
    text = tooling.read_text(encoding="utf-8")
    tooling.write_text(text.replace("stack:\n", f"stack:\n  manage_user: {value!r}\n", 1), encoding="utf-8")


def test_manage_user_defaults_to_none(minimal_config) -> None:
    assert minimal_config.stack.manage_user is None


def test_manage_user_parsed(tmp_path: Path, isolated_tooling: None) -> None:
    _write_stack_manage_user(tmp_path, "django")
    assert load_project_config(tmp_path).stack.manage_user == "django"


def test_manage_user_rejects_whitespace(tmp_path: Path, isolated_tooling: None) -> None:
    _write_stack_manage_user(tmp_path, "dj ango")
    with pytest.raises(ProjectConfigError, match="manage_user"):
        load_project_config(tmp_path)


def test_manage_exec_args_unset_keeps_image_default(minimal_config) -> None:
    assert manage_exec_args(minimal_config, ["migrate"], tty=True) == [
        "exec",
        "web",
        "./manage.py",
        "migrate",
    ]


def test_manage_exec_args_uses_configured_user(minimal_config) -> None:
    cfg = _with_manage_user(minimal_config, "django")
    assert manage_exec_args(cfg, ["migrate"], tty=False) == [
        "exec",
        "-T",
        "--user",
        "django",
        "web",
        "./manage.py",
        "migrate",
    ]


def test_manage_exec_args_override_wins(minimal_config) -> None:
    cfg = _with_manage_user(minimal_config, "django")
    args = manage_exec_args(cfg, ["shell"], tty=True, user="root")
    assert args[:3] == ["exec", "--user", "root"]


@pytest.fixture
def env_parser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    write_minimal_tooling_tree(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "local"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("name: local\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return build_dk_parser(load_project_config(tmp_path))


def test_manage_user_flag_before_args(env_parser) -> None:
    ns = env_parser.parse_args(normalize_dk_env_argv(["local", "manage", "--user", "root", "shell"]))
    assert ns.user == "root"
    assert ns.manage_args == ["shell"]


def test_manage_user_flag_after_args_goes_to_django(env_parser) -> None:
    ns = env_parser.parse_args(normalize_dk_env_argv(["local", "manage", "migrate", "--user", "x"]))
    assert ns.user is None
    assert ns.manage_args == ["migrate", "--user", "x"]


def test_post_db_restore_execs_as_manage_user(minimal_project, capsys) -> None:
    hooks = PostDbRestoreOpsConfig(envs=None, db_psql=(), manage_commands=(("migrate",),))
    cfg = replace(minimal_project, ops_optional=replace(minimal_project.ops, post_db_restore=hooks))
    cfg = _with_manage_user(cfg, "django")
    calls: list[list[str]] = []

    def fake_compose(compose_file: str, *args: str, **kwargs: object) -> MagicMock:
        calls.append(list(args))
        return MagicMock(returncode=0)

    with (
        patch("catalpa_tooling.post_db_restore._ensure_stack_volumes", return_value=0),
        patch("catalpa_tooling.post_db_restore._compose", side_effect=fake_compose),
    ):
        kwargs = dict(compose_file="compose.yml", env_add={}, env_name="dev")
        assert run_post_db_restore_manage_commands(cfg, **kwargs) == 0
        assert run_post_db_restore_manage_commands(cfg, dry_run=True, **kwargs) == 0

    assert calls[1] == ["exec", "-T", "--user", "django", "web", "./manage.py", "migrate"]
    assert "exec -T --user django web ./manage.py migrate" in capsys.readouterr().err
