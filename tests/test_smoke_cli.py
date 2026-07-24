"""Unit tests for ``tests smoke`` orchestration."""

from __future__ import annotations

from unittest.mock import patch

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
    """CI gate always uses ephemeral empty DB; never migrates primary."""
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._resolve_deploy_context", return_value=_mock_resolve(config)),
        patch("catalpa_tooling.smoke_cli._wait_for_db", return_value=True),
        patch("catalpa_tooling.smoke_cli._fresh_db_smoke", return_value=0) as fresh,
        patch("catalpa_tooling.smoke_cli._run_compose_manage", return_value=0) as manage,
        patch("catalpa_tooling.smoke_cli._run_frontend_build", return_value=0),
        patch("catalpa_tooling.smoke_cli._wait_for_web_service", return_value=True),
        patch("catalpa_tooling.smoke_cli._wait_for_frontend_url", return_value=True),
        patch("catalpa_tooling.smoke_cli._run_pytest_smoke", return_value=0),
    ):
        rc = run_smoke(config, no_up=True)
        assert rc == 0
        fresh.assert_called_once()
        # Only makemigrations --check on the default DB connection (no primary migrate).
        assert manage.call_count == 1
        assert manage.call_args.args[3:6] == ("makemigrations", "--check", "--dry-run")


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


def test_run_smoke_prepares_stack_before_up(minimal_project) -> None:
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
        patch("catalpa_tooling.smoke_cli._wait_for_db", return_value=True),
        patch("catalpa_tooling.smoke_cli._fresh_db_smoke", return_value=0),
        patch("catalpa_tooling.smoke_cli._run_compose_manage", return_value=0),
        patch("catalpa_tooling.smoke_cli._run_frontend_build", return_value=0),
        patch("catalpa_tooling.smoke_cli._wait_for_web_service", return_value=True),
        patch("catalpa_tooling.smoke_cli._wait_for_frontend_url", return_value=True),
        patch("catalpa_tooling.smoke_cli._run_pytest_smoke", return_value=0),
    ):
        rc = run_smoke(config, no_up=False)
        assert rc == 0
        prepare.assert_called_once()
        sync_proxy.assert_called_once()
        extra_files.assert_called_once()
        compose.assert_called_once()
        assert compose.call_args.kwargs.get("extra_compose_files") == [
            "/tmp/local-proxy-override.yaml"
        ]


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


def test_run_frontend_build_skips_duplicate_typecheck_when_build_chains_it(
    minimal_project, tmp_path
) -> None:
    frontend = minimal_project.frontend_dir
    frontend.mkdir(parents=True, exist_ok=True)
    (frontend / "package.json").write_text(
        '{"scripts":{"type-check":"tsc --noEmit","build":"pnpm run type-check && webpack"}}',
        encoding="utf-8",
    )
    with patch("catalpa_tooling.native_cli._run_pkg_script", return_value=0) as run_script:
        assert _run_frontend_build(minimal_project) == 0
        assert run_script.call_count == 1
        assert run_script.call_args.args[0] == "build"


def test_run_frontend_build_runs_typecheck_then_build(minimal_project) -> None:
    frontend = minimal_project.frontend_dir
    frontend.mkdir(parents=True, exist_ok=True)
    (frontend / "package.json").write_text(
        '{"scripts":{"type-check":"tsc --noEmit","build":"webpack"}}',
        encoding="utf-8",
    )
    with patch("catalpa_tooling.native_cli._run_pkg_script", return_value=0) as run_script:
        assert _run_frontend_build(minimal_project) == 0
        assert [c.args[0] for c in run_script.call_args_list] == ["type-check", "build"]


def test_run_frontend_build_prefers_compose_node_service(minimal_project) -> None:
    frontend = minimal_project.frontend_dir
    frontend.mkdir(parents=True, exist_ok=True)
    (frontend / "package.json").write_text(
        '{"scripts":{"type-check":"tsc --noEmit","build":"pnpm run type-check && webpack"}}',
        encoding="utf-8",
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
    frontend = minimal_project.frontend_dir
    frontend.mkdir(parents=True, exist_ok=True)
    (frontend / "package.json").write_text(
        '{"scripts":{"build":"webpack"}}',
        encoding="utf-8",
    )
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