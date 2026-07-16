"""Unit tests for ``tests smoke`` orchestration."""

from __future__ import annotations

from unittest.mock import patch

from catalpa_tooling.managed_deploy_env import ManagedDeployContext
from catalpa_tooling.smoke_cli import _wait_for_frontend_url, run_smoke


def _mock_deploy_context(config) -> ManagedDeployContext:
    return ManagedDeployContext(
        env_name="dev",
        compose_file="compose.yml",
        env_add={},
        docker_host="",
        backup_docker_host="",
        site_origin="http://localhost:8000",
        site_origins=("http://localhost:8000",),
        use_prepulled_registry=False,
        image_registry="",
        info_tag=None,
        config=config,
        storage_volumes={},
    )


def _mock_resolve(config):
    return ("compose.yml", {}, "http://localhost:8000", _mock_deploy_context(config), {})


def test_run_smoke_ci_ignores_fresh_db(minimal_project) -> None:
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._resolve_deploy_context", return_value=_mock_resolve(config)),
        patch("catalpa_tooling.smoke_cli._wait_for_db", return_value=True),
        patch("catalpa_tooling.smoke_cli._fresh_db_smoke", return_value=0) as fresh,
        patch("catalpa_tooling.smoke_cli._run_compose_manage", return_value=0),
        patch("catalpa_tooling.smoke_cli._wait_for_web_service", return_value=True),
        patch("catalpa_tooling.smoke_cli._wait_for_frontend_url", return_value=True),
        patch("catalpa_tooling.smoke_cli._run_pytest_smoke", return_value=0),
    ):
        rc = run_smoke(config, fresh_db=True, ci_mode=True, no_up=True)
        assert rc == 0
        fresh.assert_not_called()


def test_run_smoke_fresh_db_when_not_ci(minimal_project) -> None:
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._resolve_deploy_context", return_value=_mock_resolve(config)),
        patch("catalpa_tooling.smoke_cli._wait_for_db", return_value=True),
        patch("catalpa_tooling.smoke_cli._fresh_db_smoke", return_value=0) as fresh,
        patch("catalpa_tooling.smoke_cli._run_compose_manage", return_value=0),
        patch("catalpa_tooling.smoke_cli._wait_for_web_service", return_value=True),
        patch("catalpa_tooling.smoke_cli._wait_for_frontend_url", return_value=True),
        patch("catalpa_tooling.smoke_cli._run_pytest_smoke", return_value=0),
    ):
        rc = run_smoke(config, fresh_db=True, ci_mode=False, no_up=True)
        assert rc == 0
        fresh.assert_called_once()


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
        patch("catalpa_tooling.smoke_cli._run_compose_manage", return_value=0),
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
