"""Unit tests for ``test smoke`` orchestration."""

from __future__ import annotations

from unittest.mock import patch

from catalpa_tooling.smoke_cli import run_smoke


def test_run_smoke_ci_ignores_fresh_db(minimal_project) -> None:
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._resolve_deploy_context", return_value=("compose.yml", {}, "http://localhost:8000")),
        patch("catalpa_tooling.smoke_cli._wait_for_db", return_value=True),
        patch("catalpa_tooling.smoke_cli._fresh_db_smoke", return_value=0) as fresh,
        patch("catalpa_tooling.smoke_cli._run_compose_manage", return_value=0),
        patch("catalpa_tooling.smoke_cli._wait_for_web_service", return_value=True),
        patch("catalpa_tooling.smoke_cli._http_get_ok", return_value=True),
        patch("catalpa_tooling.smoke_cli._run_pytest_smoke", return_value=0),
    ):
        rc = run_smoke(config, fresh_db=True, ci_mode=True, no_up=True)
        assert rc == 0
        fresh.assert_not_called()


def test_run_smoke_fresh_db_when_not_ci(minimal_project) -> None:
    config = minimal_project
    with (
        patch("catalpa_tooling.smoke_cli._resolve_deploy_context", return_value=("compose.yml", {}, "http://localhost:8000")),
        patch("catalpa_tooling.smoke_cli._wait_for_db", return_value=True),
        patch("catalpa_tooling.smoke_cli._fresh_db_smoke", return_value=0) as fresh,
        patch("catalpa_tooling.smoke_cli._run_compose_manage", return_value=0),
        patch("catalpa_tooling.smoke_cli._wait_for_web_service", return_value=True),
        patch("catalpa_tooling.smoke_cli._http_get_ok", return_value=True),
        patch("catalpa_tooling.smoke_cli._run_pytest_smoke", return_value=0),
    ):
        rc = run_smoke(config, fresh_db=True, ci_mode=False, no_up=True)
        assert rc == 0
        fresh.assert_called_once()
