"""Tests for web service health polling."""

from __future__ import annotations

from unittest.mock import patch

from catalpa_tooling.compose import (
    _healthcheck_host_header,
    _healthcheck_python_snippet,
    _is_web_service_healthy,
    _wait_for_web_service,
)


def test_healthcheck_snippet_exits_quietly_on_failure() -> None:
    snippet = _healthcheck_python_snippet("http://127.0.0.1:9/")
    assert "except Exception" in snippet
    assert "traceback" not in snippet.lower()


def test_healthcheck_snippet_sends_host_header() -> None:
    snippet = _healthcheck_python_snippet(
        "http://localhost:8000/cms/",
        host_header="khs.dev.localhost",
    )
    assert "Request(" in snippet
    assert "'Host': 'khs.dev.localhost'" in snippet


def test_healthcheck_host_header_from_bero_origin() -> None:
    assert _healthcheck_host_header({"BERO_ORIGIN": "http://khs.dev.localhost:9004"}) == "khs.dev.localhost"
    assert _healthcheck_host_header({"SITE_ORIGIN": "http://localhost:9001"}) == "localhost"
    assert _healthcheck_host_header({}) is None


def test_is_web_service_healthy_passes_env_add(minimal_project) -> None:
    env_add = {"DOCKER_HOST": "ssh://example", "COMPOSE_PROJECT_NAME": "myproj"}
    with patch("catalpa_tooling.compose._compose") as compose:
        compose.return_value.returncode = 0
        assert _is_web_service_healthy(
            "compose.yml", minimal_project, env_add=env_add
        )
        compose.assert_called_once()
        assert compose.call_args.kwargs.get("env_add") == env_add


def test_wait_for_web_service_passes_env_add(minimal_project) -> None:
    env_add = {"COMPOSE_PROJECT_NAME": "myproj"}
    with patch(
        "catalpa_tooling.compose._is_web_service_healthy",
        return_value=True,
    ) as healthy:
        assert _wait_for_web_service(
            "compose.yml", minimal_project, env_add=env_add, timeout_seconds=1
        )
        healthy.assert_called_once_with(
            "compose.yml", minimal_project, env_add=env_add
        )
