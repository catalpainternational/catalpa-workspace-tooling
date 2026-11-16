"""Unit tests for ``tests smoke`` orchestration."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from catalpa_tooling.managed_deploy_env import ManagedDeployContext
from catalpa_tooling.smoke_cli import (
    _lookup_db_user,
    _pytest_selects_elearning,
    _resolve_db_owner,
    _run_frontend_build,
    _wait_for_frontend_url,
    functional_pytest_args,
    run_smoke,
)


def _mock_deploy_context(config) -> ManagedDeployContext:
    return ManagedDeployContext(
        env_name="dev",
        compose_file="compose.yml",
        env_add={},
        docker_host="",
        dc_backup_docker_host="",
        site_origin="http://localhost:8000",
        site_origins=("http://localhost:8000",),
        use_prepulled_registry=False,
        image_registry="",
        info_tag=None,
        config=config,
        storage_volumes={},
        info={},
    )


def _mock_resolve(config):
    return ("compose.yml", {}, "http://localhost:8000", _mock_deploy_context(config), {})


def test_run_smoke_gate_always_empty_migrates(minimal_project) -> None:
    """CI gate always uses ephemeral empty DB; never migrates primary; no Playwright."""
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._resolve_deploy_context", return_value=_mock_resolve(config)),
        patch("catalpa_tooling.smoke_cli._wait_for_db", return_value=True),
        patch("catalpa_tooling.smoke_cli._fresh_db_smoke", return_value=0) as fresh,
        patch("catalpa_tooling.smoke_cli._run_compose_manage", return_value=0) as manage,
        patch("catalpa_tooling.smoke_cli._run_frontend_build", return_value=0),
        patch("catalpa_tooling.smoke_cli._wait_for_web_service") as wait_web,
        patch("catalpa_tooling.smoke_cli._wait_for_frontend_url") as wait_fe,
        patch("catalpa_tooling.smoke_cli._run_pytest_smoke") as pytest_smoke,
    ):
        rc = run_smoke(config, no_up=True)
        assert rc == 0
        fresh.assert_called_once()
        # Only makemigrations --check on the default DB connection (no primary migrate).
        assert manage.call_count == 1
        assert manage.call_args.args[3:6] == ("makemigrations", "--check", "--dry-run")
        wait_web.assert_not_called()
        wait_fe.assert_not_called()
        pytest_smoke.assert_not_called()


def test_run_smoke_functional_skips_gate(minimal_project) -> None:
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._resolve_deploy_context", return_value=_mock_resolve(config)),
        patch("catalpa_tooling.smoke_cli._wait_for_db") as wait_db,
        patch("catalpa_tooling.smoke_cli._fresh_db_smoke") as fresh,
        patch("catalpa_tooling.smoke_cli._run_compose_manage") as manage,
        patch("catalpa_tooling.smoke_cli._run_frontend_build") as build,
        patch("catalpa_tooling.smoke_cli._wait_for_web_service", return_value=True),
        patch("catalpa_tooling.smoke_cli._wait_for_frontend_url", return_value=True),
        patch("catalpa_tooling.smoke_cli._run_pytest_smoke", return_value=0) as pytest_smoke,
    ):
        rc = run_smoke(config, functional=True, no_up=True, pytest_args=["-m", "elearning"])
        assert rc == 0
        wait_db.assert_not_called()
        fresh.assert_not_called()
        manage.assert_not_called()
        build.assert_not_called()
        pytest_smoke.assert_called_once()


def test_run_smoke_guest_skips_gate(minimal_project) -> None:
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._resolve_deploy_context", return_value=_mock_resolve(config)),
        patch("catalpa_tooling.smoke_cli._fresh_db_smoke") as fresh,
        patch("catalpa_tooling.smoke_cli._run_frontend_build") as build,
        patch("catalpa_tooling.smoke_cli._wait_for_web_service", return_value=True),
        patch("catalpa_tooling.smoke_cli._wait_for_frontend_url", return_value=True),
        patch("catalpa_tooling.smoke_cli._run_pytest_smoke", return_value=0) as pytest_smoke,
    ):
        rc = run_smoke(config, guest=True, no_up=True)
        assert rc == 0
        fresh.assert_not_called()
        build.assert_not_called()
        pytest_smoke.assert_called_once()


def test_run_smoke_elearning_marker_implies_functional(minimal_project) -> None:
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._resolve_deploy_context", return_value=_mock_resolve(config)),
        patch("catalpa_tooling.smoke_cli._run_compose_manage") as manage,
        patch("catalpa_tooling.smoke_cli._run_frontend_build") as build,
        patch("catalpa_tooling.smoke_cli._wait_for_web_service", return_value=True),
        patch("catalpa_tooling.smoke_cli._wait_for_frontend_url", return_value=True),
        patch("catalpa_tooling.smoke_cli._run_pytest_smoke", return_value=0),
    ):
        rc = run_smoke(config, no_up=True, pytest_args=["-m", "elearning", "--headed"])
        assert rc == 0
        manage.assert_not_called()
        build.assert_not_called()


def test_pytest_selects_elearning() -> None:
    assert _pytest_selects_elearning(["-m", "elearning"]) is True
    assert _pytest_selects_elearning(["-m", "elearning and not slow"]) is True
    assert _pytest_selects_elearning(["-m", "not elearning"]) is False
    assert _pytest_selects_elearning(["-m", "smoke"]) is False
    assert _pytest_selects_elearning([]) is False


def test_wait_for_frontend_url_retries_until_success() -> None:
    attempts = {"n": 0}

    def fake_detail(url: str, *, timeout: float = 10.0):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return False, "TimeoutError:timed out"
        return True, None

    with (
        patch("catalpa_tooling.smoke_cli._http_get_detail", side_effect=fake_detail),
        patch("catalpa_tooling.smoke_cli.time.sleep"),
    ):
        assert _wait_for_frontend_url("http://example.test/", timeout_seconds=30, poll_interval=1) is True
        assert attempts["n"] == 3


def test_run_compose_manage_uses_run_no_deps(minimal_project) -> None:
    from catalpa_tooling.smoke_cli import _run_compose_manage

    config = minimal_project
    with patch(
        "catalpa_tooling.smoke_cli._compose",
        return_value=type("R", (), {"returncode": 0})(),
    ) as compose:
        assert (
            _run_compose_manage(
                "compose.yml",
                config,
                {},
                "migrate",
                "--noinput",
                extra_exec_env={"DJANGO_DB": "bero_db_smoke_empty", "POSTGRES_DB": "bero_db_smoke_empty"},
            )
            == 0
        )
        argv = list(compose.call_args.args[1:])
        assert argv[:4] == ["run", "--rm", "-T", "--no-deps"]
        assert "-e" in argv
        assert f"{config.stack_service('web')}" in argv
        assert "./manage.py" in argv
        assert "migrate" in argv


def test_run_smoke_ci_lean_up_skips_proxy(minimal_project) -> None:
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._resolve_deploy_context", return_value=_mock_resolve(config)),
        patch("catalpa_tooling.smoke_cli._prepare_compose_up", return_value=0) as prepare,
        patch("catalpa_tooling.smoke_cli.sync_local_proxy_for_compose_action") as sync_proxy,
        patch("catalpa_tooling.smoke_cli._compose", return_value=type("R", (), {"returncode": 0})()) as compose,
        patch(
            "catalpa_tooling.smoke_cli.write_label_override",
            return_value="/tmp/catalpa-stack-labels.yaml",
        ),
        patch("catalpa_tooling.smoke_cli.os.unlink"),
        patch("catalpa_tooling.smoke_cli._wait_for_db", return_value=True),
        patch("catalpa_tooling.smoke_cli._fresh_db_smoke", return_value=0),
        patch("catalpa_tooling.smoke_cli._run_compose_manage", return_value=0),
        patch("catalpa_tooling.smoke_cli._run_frontend_build", return_value=0),
        patch("catalpa_tooling.smoke_cli._run_pytest_smoke") as pytest_smoke,
    ):
        rc = run_smoke(config, no_up=False)
        assert rc == 0
        prepare.assert_called_once()
        assert prepare.call_args.kwargs.get("lean_ci") is True
        sync_proxy.assert_not_called()
        compose.assert_called_once()
        up_args = compose.call_args.args[1:]
        assert up_args[0] == "up"
        assert config.stack_service("db") in up_args
        assert config.stack_service("web") not in up_args
        # db only: no proxy override (caddy is never touched), but the build is labelled.
        assert compose.call_args.kwargs.get("extra_compose_files") == [
            "/tmp/catalpa-stack-labels.yaml"
        ]
        pytest_smoke.assert_not_called()


def test_label_override_is_deleted_when_compose_raises(minimal_project) -> None:
    from catalpa_tooling.smoke_cli import _label_override_for

    with (
        patch(
            "catalpa_tooling.smoke_cli.write_label_override",
            return_value="/tmp/catalpa-stack-labels.yaml",
        ),
        patch("catalpa_tooling.smoke_cli.os.unlink") as unlink,
    ):
        with pytest.raises(RuntimeError):
            with _label_override_for(
                minimal_project, "compose.yml", {}, ["up", "-d", "--build"]
            ) as files:
                assert files == ["/tmp/catalpa-stack-labels.yaml"]
                raise RuntimeError("compose blew up")
    unlink.assert_called_once_with("/tmp/catalpa-stack-labels.yaml")


def test_run_smoke_unreadable_worktree_marker_fails_cleanly(
    tmp_path, isolated_tooling, capsys
) -> None:
    """A malformed marker is a clean failure, not a traceback (review on #169)."""
    from catalpa_tooling.config import load_project_config
    from tests.helpers import write_minimal_tooling_tree

    write_minimal_tooling_tree(tmp_path)
    (tmp_path / ".catalpa-worktree.yaml").write_text("slug: [unclosed\n", encoding="utf-8")
    config = load_project_config(tmp_path)
    info_dir = config.deploy_envs_dir / "dev"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "info.yaml").write_text(
        "docker_host: ''\ncompose_file: compose.yml\n", encoding="utf-8"
    )
    (tmp_path / "compose.yml").write_text("services: {}\n", encoding="utf-8")

    assert run_smoke(config, no_up=True, log_prefix="ci") == 1
    assert "cannot read worktree marker" in capsys.readouterr().err


def test_build_lean_ci_images_are_labelled(minimal_project) -> None:
    """Unlabelled CI images look stale to the next `dk` command, which recreates the stack (#77)."""
    from catalpa_tooling.smoke_cli import _build_lean_ci_images

    config = minimal_project
    with (
        patch(
            "catalpa_tooling.smoke_cli._compose_service_names",
            return_value=frozenset({"db", config.stack_service("web"), "node"}),
        ),
        patch("catalpa_tooling.smoke_cli.compose_yml_build", return_value=0) as build,
    ):
        assert (
            _build_lean_ci_images(
                "compose.dev.yaml", config, {}, use_prepulled_registry=False
            )
            == 0
        )
    build.assert_called_once()
    assert build.call_args.kwargs["compose_file"] == "compose.dev.yaml"
    assert build.call_args.kwargs["services"] == (
        config.stack_service("db"),
        config.stack_service("web"),
        "node",
    )


def test_run_smoke_guest_prepares_full_stack_with_proxy(minimal_project) -> None:
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._resolve_deploy_context", return_value=_mock_resolve(config)),
        patch("catalpa_tooling.smoke_cli._prepare_compose_up", return_value=0) as prepare,
        patch("catalpa_tooling.smoke_cli.sync_local_proxy_for_compose_action", return_value=0) as sync_proxy,
        patch(
            "catalpa_tooling.smoke_cli.local_proxy_extra_compose_files",
            return_value=["/tmp/local-proxy-override.yaml"],
        ) as extra_files,
        patch("catalpa_tooling.smoke_cli._compose", return_value=type("R", (), {"returncode": 0})()) as compose,
        patch(
            "catalpa_tooling.smoke_cli.write_label_override",
            return_value="/tmp/catalpa-stack-labels.yaml",
        ),
        patch("catalpa_tooling.smoke_cli.os.unlink") as unlink,
        patch("catalpa_tooling.smoke_cli._wait_for_web_service", return_value=True),
        patch("catalpa_tooling.smoke_cli._wait_for_frontend_url", return_value=True),
        patch("catalpa_tooling.smoke_cli._run_pytest_smoke", return_value=0),
    ):
        rc = run_smoke(config, guest=True, no_up=False)
        assert rc == 0
        prepare.assert_called_once()
        assert prepare.call_args.kwargs.get("lean_ci", False) is False
        sync_proxy.assert_called_once()
        extra_files.assert_called_once()
        compose.assert_called_once()
        assert "--build" in compose.call_args.args
        # The `up --build` is labelled like `dk dev up`'s, so the next dk command doesn't see a
        # stale stack and recreate it (#77, #167).
        assert compose.call_args.kwargs.get("extra_compose_files") == [
            "/tmp/local-proxy-override.yaml",
            "/tmp/catalpa-stack-labels.yaml",
        ]
        unlink.assert_called_once_with("/tmp/catalpa-stack-labels.yaml")


def test_lookup_db_user_skips_postgres_superuser(minimal_project) -> None:
    config = minimal_project
    assert _lookup_db_user({"POSTGRES_USER": "postgres"}, config) is None
    assert _lookup_db_user({"POSTGRES_USER": "postgres", "DJANGO_DB_USER": "bero"}, config) == "bero"


def test_resolve_db_owner_prefers_env_add(minimal_project) -> None:
    config = minimal_project
    with patch("catalpa_tooling.smoke_cli._compose_printenv") as printenv:
        owner = _resolve_db_owner(
            "compose.yml",
            config,
            {"DJANGO_DB_USER": "bero"},
            primary_db="bero_db",
        )
        assert owner == "bero"
        printenv.assert_not_called()


def test_resolve_db_owner_from_container_then_primary(minimal_project) -> None:
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._lookup_db_user", return_value=None),
        patch("catalpa_tooling.smoke_cli._compose_printenv", return_value=None),
        patch("catalpa_tooling.smoke_cli._psql_scalar", return_value="bero") as scalar,
    ):
        owner = _resolve_db_owner("compose.yml", config, {}, primary_db="bero_db")
        assert owner == "bero"
        scalar.assert_called_once()


def test_resolve_db_owner_never_uses_project_name(minimal_project) -> None:
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._lookup_db_user", return_value=None),
        patch("catalpa_tooling.smoke_cli._compose_printenv", return_value=None),
        patch("catalpa_tooling.smoke_cli._psql_scalar", return_value=None),
    ):
        assert _resolve_db_owner("compose.yml", config, {}, primary_db="bero_db") is None


def test_functional_pytest_args_defaults_marker_and_headed_slowmo() -> None:
    assert functional_pytest_args(headed=False) == ["-m", "elearning"]
    assert functional_pytest_args(headed=True, slowmo_ms=250) == [
        "-m",
        "elearning",
        "--headed",
        "--slowmo=250",
    ]
    assert functional_pytest_args(
        headed=True,
        extra=["-m", "elearning", "--slowmo=100"],
        slowmo_ms=250,
    ) == ["-m", "elearning", "--slowmo=100", "--headed"]


def test_parse_functional_rest_headed_and_flags() -> None:
    from catalpa_tooling.test_cli import parse_functional_rest

    headed, no_up, env, extra = parse_functional_rest(
        ["headed", "--no-up", "--", "-k", "lesson"],
        headed_flag=False,
        no_up_flag=False,
        env_flag="dev",
    )
    assert (headed, no_up, env, extra) == (True, True, "dev", ["-k", "lesson"])


def _write_frontend(directory, package_json: str, *, deps: bool = True) -> None:
    """Create a frontend directory with ``package.json`` and (by default) ``node_modules``."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package.json").write_text(package_json, encoding="utf-8")
    if deps:
        (directory / "node_modules").mkdir(exist_ok=True)


def test_run_frontend_build_skips_duplicate_typecheck_when_build_chains_it(
    minimal_project, tmp_path
) -> None:
    _write_frontend(
        minimal_project.frontend_dir,
        '{"scripts":{"type-check":"tsc --noEmit","build":"pnpm run type-check && webpack"}}',
    )
    with patch("catalpa_tooling.native_cli._run_pkg_script", return_value=0) as run_script:
        assert _run_frontend_build(minimal_project) == 0
        assert run_script.call_count == 1
        assert run_script.call_args.args[0] == "build"


def test_run_frontend_build_runs_typecheck_then_build(minimal_project) -> None:
    _write_frontend(
        minimal_project.frontend_dir,
        '{"scripts":{"type-check":"tsc --noEmit","build":"webpack"}}',
    )
    with patch("catalpa_tooling.native_cli._run_pkg_script", return_value=0) as run_script:
        assert _run_frontend_build(minimal_project) == 0
        assert [c.args[0] for c in run_script.call_args_list] == ["type-check", "build"]


def test_run_frontend_build_prefers_compose_node_service(minimal_project) -> None:
    _write_frontend(
        minimal_project.frontend_dir,
        '{"scripts":{"type-check":"tsc --noEmit","build":"pnpm run type-check && webpack"}}',
    )
    with (
        patch(
            "catalpa_tooling.smoke_cli._compose_service_names",
            return_value=frozenset({"django", "node", "db"}),
        ),
        patch(
            "catalpa_tooling.smoke_cli._run_frontend_script_in_compose",
            return_value=0,
        ) as compose_run,
        patch("catalpa_tooling.native_cli._run_pkg_script") as host_run,
    ):
        assert (
            _run_frontend_build(
                minimal_project,
                compose_file="compose.dev.yaml",
                env_add={"COMPOSE_PROJECT_NAME": "x"},
            )
            == 0
        )
        compose_run.assert_called_once()
        assert compose_run.call_args.kwargs["script"] == "build"
        assert compose_run.call_args.kwargs["service"] == "node"
        host_run.assert_not_called()


def test_run_frontend_build_falls_back_to_host_without_node_service(
    minimal_project,
) -> None:
    _write_frontend(minimal_project.frontend_dir, '{"scripts":{"build":"webpack"}}')
    with (
        patch(
            "catalpa_tooling.smoke_cli._compose_service_names",
            return_value=frozenset({"django", "db"}),
        ),
        patch(
            "catalpa_tooling.smoke_cli._run_frontend_script_in_compose",
        ) as compose_run,
        patch("catalpa_tooling.native_cli._run_pkg_script", return_value=0) as host_run,
    ):
        assert (
            _run_frontend_build(
                minimal_project,
                compose_file="compose.yaml",
                env_add={},
            )
            == 0
        )
        compose_run.assert_not_called()
        host_run.assert_called_once()
        assert host_run.call_args.args[0] == "build"


def _two_frontend_project(tmp_path):
    """``minimal_project`` re-declared with two frontends, both with a build script.

    ``web`` is the primary (compose ``node`` service, when one exists); ``admin``
    is the extra that 1.3.2 and earlier could not reach at all.
    """
    from catalpa_tooling.config import load_project_config

    from tests.helpers import write_minimal_tooling_tree

    write_minimal_tooling_tree(tmp_path)
    manifest = tmp_path / "tooling.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "  frontend: frontend\n",
            "  frontend:\n    - web\n    - admin\n",
        ),
        encoding="utf-8",
    )
    config = load_project_config(tmp_path)
    for name in ("web", "admin"):
        _write_frontend(
            tmp_path / name,
            '{"scripts":{"type-check":"tsc --noEmit","build":"webpack"}}',
        )
    return config


def test_run_frontend_build_builds_every_declared_frontend(tmp_path) -> None:
    config = _two_frontend_project(tmp_path)
    with patch("catalpa_tooling.native_cli._run_pkg_script", return_value=0) as run_script:
        assert _run_frontend_build(config) == 0
        assert [(c.args[0], c.args[1].name) for c in run_script.call_args_list] == [
            ("type-check", "web"),
            ("build", "web"),
            ("type-check", "admin"),
            ("build", "admin"),
        ]


def test_run_frontend_build_stops_at_the_first_failing_frontend(tmp_path) -> None:
    config = _two_frontend_project(tmp_path)
    with patch("catalpa_tooling.native_cli._run_pkg_script", return_value=2) as run_script:
        assert _run_frontend_build(config) == 2
        # The primary's type-check failed, so nothing else ran — not its own
        # build, and not the second frontend at all.
        assert [c.args[0] for c in run_script.call_args_list] == ["type-check"]


def test_run_frontend_build_resolves_package_manager_per_frontend(tmp_path) -> None:
    """A second frontend may use a different package manager from the first."""
    config = _two_frontend_project(tmp_path)
    (tmp_path / "web" / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (tmp_path / "admin" / "yarn.lock").write_text("# yarn lockfile v1\n", encoding="utf-8")
    with patch("catalpa_tooling.native_cli._run_pkg_script", return_value=0) as run_script:
        assert _run_frontend_build(config) == 0
        by_dir = {c.args[1].name: c.args[2] for c in run_script.call_args_list}
        assert by_dir == {"web": "pnpm", "admin": "yarn"}


def test_run_frontend_build_uses_compose_node_service_for_the_primary_only(tmp_path) -> None:
    """The ``node`` service is wired to one directory, so extras run on the host.

    Routing a second frontend through it would build the primary twice and report
    it under the second one's name.
    """
    config = _two_frontend_project(tmp_path)
    with (
        patch(
            "catalpa_tooling.smoke_cli._compose_service_names",
            return_value=frozenset({"django", "node", "db"}),
        ),
        patch(
            "catalpa_tooling.smoke_cli._run_frontend_script_in_compose",
            return_value=0,
        ) as compose_run,
        patch("catalpa_tooling.native_cli._run_pkg_script", return_value=0) as host_run,
    ):
        assert (
            _run_frontend_build(
                config,
                compose_file="compose.dev.yaml",
                env_add={"COMPOSE_PROJECT_NAME": "x"},
            )
            == 0
        )
        assert [c.kwargs["script"] for c in compose_run.call_args_list] == [
            "type-check",
            "build",
        ]
        assert [(c.args[0], c.args[1].name) for c in host_run.call_args_list] == [
            ("type-check", "admin"),
            ("build", "admin"),
        ]


def test_run_frontend_build_skips_a_frontend_with_no_build_scripts(tmp_path) -> None:
    """A declared directory with no type-check/build is skipped, not fatal."""
    config = _two_frontend_project(tmp_path)
    (tmp_path / "admin" / "package.json").write_text(
        '{"scripts":{"dev":"vite"}}',
        encoding="utf-8",
    )
    with patch("catalpa_tooling.native_cli._run_pkg_script", return_value=0) as run_script:
        assert _run_frontend_build(config) == 0
        assert [c.args[1].name for c in run_script.call_args_list] == ["web", "web"]

def _worktree_project(tmp_path):
    from catalpa_tooling.config import load_project_config
    from catalpa_tooling.worktree_overlay import WorktreeOverlay, write_worktree_overlay
    from tests.helpers import write_minimal_tooling_tree

    write_minimal_tooling_tree(tmp_path)
    write_worktree_overlay(
        tmp_path,
        WorktreeOverlay(
            version=1,
            slug="img337",
            base_env="dev",
            compose_project_name="app_compose_dev_img337",
            site_origin="https://minimal-dev-img337.localdev.temp.build",
            parent_repo_root="/main/checkout",
            branch="worktree/img337",
        ),
    )
    return load_project_config(tmp_path)


def test_run_smoke_runs_inside_a_worktree(tmp_path, isolated_tooling) -> None:
    """`tests ci` in a worktree runs against the worktree's stack instead of refusing (#167)."""
    config = _worktree_project(tmp_path)
    with (
        patch(
            "catalpa_tooling.smoke_cli._resolve_deploy_context",
            return_value=_mock_resolve(config),
        ) as resolve,
        patch("catalpa_tooling.smoke_cli._wait_for_db", return_value=True),
        patch("catalpa_tooling.smoke_cli._fresh_db_smoke", return_value=0),
        patch("catalpa_tooling.smoke_cli._run_compose_manage", return_value=0),
        patch("catalpa_tooling.smoke_cli._run_frontend_build", return_value=0),
    ):
        assert run_smoke(config, no_up=True, log_prefix="ci") == 0
    resolve.assert_called_once_with(config, "dev")


def test_run_smoke_worktree_uses_overlay_project_and_origin(
    tmp_path, isolated_tooling
) -> None:
    """The deploy context `run_smoke` builds from a worktree carries the overlay (#167)."""
    from catalpa_tooling.smoke_cli import _resolve_deploy_context

    config = _worktree_project(tmp_path)
    info_dir = config.deploy_envs_dir / "dev"
    info_dir.mkdir(parents=True, exist_ok=True)
    (info_dir / "info.yaml").write_text(
        "docker_host: ''\ncompose_file: compose.yml\nsite_origin: https://minimal-dev.localdev.temp.build\n",
        encoding="utf-8",
    )
    (config.repo_root / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    with patch.object(type(config), "credentials_optional_for_env", return_value=True):
        resolved = _resolve_deploy_context(config, "dev")
    assert resolved is not None
    _compose_file, env_add, site_origin, _ctx, info = resolved
    assert env_add["COMPOSE_PROJECT_NAME"] == "app_compose_dev_img337"
    assert site_origin == "https://minimal-dev-img337.localdev.temp.build"
    assert info["site_origin"] == site_origin


def test_run_smoke_refuses_other_env_inside_a_worktree(
    tmp_path, isolated_tooling, capsys
) -> None:
    """The overlay remaps only its base env; `--env full` would hit the main stack (#105)."""
    config = _worktree_project(tmp_path)

    with patch("catalpa_tooling.smoke_cli._resolve_deploy_context") as resolve:
        assert run_smoke(config, env_name="full", guest=True, no_up=True, log_prefix="guest") == 2
        resolve.assert_not_called()

    err = capsys.readouterr().err
    assert "img337" in err
    assert "'dev'" in err
    assert "--env full" in err
    assert "/main/checkout" in err
    assert "#105" in err


def test_run_frontend_build_host_fallback_requires_node_modules(
    minimal_project, capsys
) -> None:
    """The host path assumed deps; a fresh checkout died on a missing binary (#80)."""
    _write_frontend(
        minimal_project.frontend_dir,
        '{"scripts":{"type-check":"tsc --noEmit","build":"webpack"}}',
        deps=False,
    )
    with (
        patch(
            "catalpa_tooling.smoke_cli._compose_service_names",
            return_value=frozenset({"django", "db"}),
        ),
        patch("catalpa_tooling.native_cli._run_pkg_script") as host_run,
    ):
        rc = _run_frontend_build(
            minimal_project,
            compose_file="compose.yaml",
            env_add={},
        )

    assert rc != 0
    host_run.assert_not_called()
    err = capsys.readouterr().err
    assert "node_modules missing" in err
    assert "install --frozen-lockfile" in err


def test_run_frontend_build_compose_path_ignores_missing_node_modules(
    minimal_project,
) -> None:
    """node_modules lives in the image on the compose path, so no host check applies."""
    _write_frontend(
        minimal_project.frontend_dir,
        '{"scripts":{"build":"webpack"}}',
        deps=False,
    )
    with (
        patch(
            "catalpa_tooling.smoke_cli._compose_service_names",
            return_value=frozenset({"django", "node", "db"}),
        ),
        patch(
            "catalpa_tooling.smoke_cli._run_frontend_script_in_compose",
            return_value=0,
        ) as compose_run,
    ):
        assert (
            _run_frontend_build(
                minimal_project,
                compose_file="compose.yaml",
                env_add={},
            )
            == 0
        )
    compose_run.assert_called_once()
