"""Tests for post-restore Django management command runner."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from catalpa_tooling.config import PostDbRestoreOpsConfig
from catalpa_tooling.post_db_restore import run_post_db_restore_manage_commands


def _config_with_hooks(minimal_project, *, envs=None, commands=()):
    from dataclasses import replace

    hooks = PostDbRestoreOpsConfig(envs=envs, db_psql=(), manage_commands=commands)
    ops = replace(minimal_project.ops, post_db_restore=hooks)
    return replace(minimal_project, ops_optional=ops)


def test_skips_when_no_commands(minimal_project) -> None:
    with patch("catalpa_tooling.post_db_restore._compose") as compose:
        assert (
            run_post_db_restore_manage_commands(
                minimal_project,
                compose_file="compose.yml",
                env_add={},
                env_name="local",
            )
            == 0
        )
        compose.assert_not_called()


def test_skips_when_env_not_in_allowlist(minimal_project) -> None:
    cfg = _config_with_hooks(
        minimal_project,
        envs=("staging",),
        commands=(("migrate",),),
    )
    with patch("catalpa_tooling.post_db_restore._compose") as compose:
        assert (
            run_post_db_restore_manage_commands(
                cfg,
                compose_file="compose.yml",
                env_add={},
                env_name="local",
            )
            == 0
        )
        compose.assert_not_called()


def test_runs_commands_in_order(minimal_project) -> None:
    cfg = _config_with_hooks(
        minimal_project,
        commands=(
            ("sync_wagtail_sites", "--profile", "dev"),
            ("migrate", "--noinput"),
        ),
    )
    calls: list[list[str]] = []

    def fake_compose(compose_file: str, *args: str, **kwargs: object) -> MagicMock:
        calls.append(list(args))
        m = MagicMock()
        m.returncode = 0
        return m

    with (
        patch(
            "catalpa_tooling.post_db_restore._ensure_stack_volumes",
            return_value=0,
        ) as ensure_volumes,
        patch("catalpa_tooling.post_db_restore._compose", side_effect=fake_compose),
    ):
        assert (
            run_post_db_restore_manage_commands(
                cfg,
                compose_file="compose.yml",
                env_add={"COMPOSE_PROJECT_NAME": "app"},
                env_name="dev",
            )
            == 0
        )

    ensure_volumes.assert_called_once()
    assert calls[0] == ["up", "-d", "web", "--wait"]
    assert calls[1][:4] == ["exec", "-T", "web", "./manage.py"]
    assert calls[1][4:] == ["sync_wagtail_sites", "--profile", "dev"]
    assert calls[2][4:] == ["migrate", "--noinput"]


def test_expands_env_name_placeholder(minimal_project) -> None:
    cfg = _config_with_hooks(
        minimal_project,
        commands=(("sync_wagtail_sites", "--profile", "{env_name}"),),
    )
    exec_args: list[str] = []

    def fake_compose(compose_file: str, *args: str, **kwargs: object) -> MagicMock:
        if args and args[0] == "exec":
            exec_args.extend(args)
        m = MagicMock()
        m.returncode = 0
        return m

    with (
        patch("catalpa_tooling.post_db_restore._ensure_stack_volumes", return_value=0),
        patch("catalpa_tooling.post_db_restore._compose", side_effect=fake_compose),
    ):
        run_post_db_restore_manage_commands(
            cfg,
            compose_file="compose.yml",
            env_add={},
            env_name="staging",
        )

    assert "staging" in exec_args
    assert "{env_name}" not in exec_args


def test_propagates_first_failure(minimal_project) -> None:
    cfg = _config_with_hooks(
        minimal_project,
        commands=(("migrate",), ("shell",)),
    )

    def fake_compose(compose_file: str, *args: str, **kwargs: object) -> MagicMock:
        m = MagicMock()
        if args and args[0] == "exec":
            m.returncode = 2
        else:
            m.returncode = 0
        return m

    with (
        patch("catalpa_tooling.post_db_restore._ensure_stack_volumes", return_value=0),
        patch("catalpa_tooling.post_db_restore._compose", side_effect=fake_compose),
    ):
        assert (
            run_post_db_restore_manage_commands(
                cfg,
                compose_file="compose.yml",
                env_add={},
                env_name="local",
            )
            == 2
        )


def test_ensures_stack_volumes_before_compose_up(minimal_project) -> None:
    cfg = _config_with_hooks(minimal_project, commands=(("migrate",),))
    call_order: list[str] = []

    def fake_ensure(*args: object, **kwargs: object) -> int:
        call_order.append("ensure")
        return 0

    def fake_compose(compose_file: str, *args: str, **kwargs: object) -> MagicMock:
        call_order.append("compose")
        m = MagicMock()
        m.returncode = 0
        return m

    with (
        patch(
            "catalpa_tooling.post_db_restore._ensure_stack_volumes",
            side_effect=fake_ensure,
        ),
        patch("catalpa_tooling.post_db_restore._compose", side_effect=fake_compose),
    ):
        assert (
            run_post_db_restore_manage_commands(
                cfg,
                compose_file="compose.yml",
                env_add={"COMPOSE_PROJECT_NAME": "app"},
                env_name="dev",
            )
            == 0
        )

    assert call_order[:2] == ["ensure", "compose"]


def test_dry_run_ensures_volumes_before_plan(minimal_project, capsys) -> None:
    cfg = _config_with_hooks(minimal_project, commands=(("migrate",),))
    with (
        patch(
            "catalpa_tooling.post_db_restore._ensure_stack_volumes",
            return_value=0,
        ) as ensure_volumes,
        patch("catalpa_tooling.post_db_restore._compose") as compose,
    ):
        assert (
            run_post_db_restore_manage_commands(
                cfg,
                compose_file="compose.yml",
                env_add={},
                env_name="local",
                dry_run=True,
            )
            == 0
        )
        compose.assert_not_called()
    ensure_volumes.assert_called_once_with(
        cfg,
        "local",
        {},
        dry_run=True,
    )
    err = capsys.readouterr().err
    assert "dry-run" in err
    assert "manage.py migrate" in err


def test_propagates_volume_ensure_failure(minimal_project) -> None:
    cfg = _config_with_hooks(minimal_project, commands=(("migrate",),))
    with (
        patch(
            "catalpa_tooling.post_db_restore._ensure_stack_volumes",
            return_value=1,
        ),
        patch("catalpa_tooling.post_db_restore._compose") as compose,
    ):
        assert (
            run_post_db_restore_manage_commands(
                cfg,
                compose_file="compose.yml",
                env_add={},
                env_name="local",
            )
            == 1
        )
        compose.assert_not_called()


def test_db_psql_runs_before_manage_commands(minimal_project) -> None:
    from dataclasses import replace

    from catalpa_tooling.config import DbPsqlRestoreEntry, PostDbRestoreOpsConfig

    hooks = PostDbRestoreOpsConfig(
        envs=None,
        db_psql=(DbPsqlRestoreEntry(target="app", file="fix.sql"),),
        manage_commands=(("migrate",),),
    )
    cfg = replace(minimal_project, ops_optional=replace(minimal_project.ops, post_db_restore=hooks))
    call_order: list[str] = []

    def fake_db_psql(*args: object, **kwargs: object) -> int:
        call_order.append("db_psql")
        return 0

    def fake_compose(compose_file: str, *args: str, **kwargs: object) -> MagicMock:
        call_order.append("compose")
        m = MagicMock()
        m.returncode = 0
        return m

    with (
        patch(
            "catalpa_tooling.post_db_restore.run_db_psql_hooks",
            side_effect=fake_db_psql,
        ),
        patch(
            "catalpa_tooling.post_db_restore._ensure_stack_volumes",
            return_value=0,
        ),
        patch("catalpa_tooling.post_db_restore._compose", side_effect=fake_compose),
    ):
        assert (
            run_post_db_restore_manage_commands(
                cfg,
                compose_file="compose.yml",
                env_add={},
                env_name="dev",
            )
            == 0
        )

    assert call_order[0] == "db_psql"
    assert "compose" in call_order


def test_db_psql_only_skips_compose(minimal_project, tmp_path) -> None:
    from dataclasses import replace

    from catalpa_tooling.config import DbPsqlRestoreEntry, PostDbRestoreOpsConfig

    hooks = PostDbRestoreOpsConfig(
        envs=None,
        db_psql=(DbPsqlRestoreEntry(target="app", file="fix.sql"),),
        manage_commands=(),
    )
    cfg = replace(minimal_project, repo_root=tmp_path)
    cfg = replace(cfg, ops_optional=replace(cfg.ops, post_db_restore=hooks))
    (tmp_path / "fix.sql").write_text("SELECT 1;\n", encoding="utf-8")

    with (
        patch(
            "catalpa_tooling.post_db_restore._resolve_db_psql_container_path",
            return_value=(0, "/tmp/fix.sql"),
        ),
        patch(
            "catalpa_tooling.post_db_restore.run_cmd",
            return_value=MagicMock(returncode=0),
        ),
        patch("catalpa_tooling.post_db_restore._compose") as compose,
    ):
        assert (
            run_post_db_restore_manage_commands(
                cfg,
                compose_file="compose.yml",
                env_add={},
                env_name="dev",
            )
            == 0
        )
        compose.assert_not_called()