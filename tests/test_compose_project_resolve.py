"""Tests for ``resolve_env_with_compose_project`` (Compose / dk volume prefix behaviour)."""

import pytest

from catalpa_tooling.restic_files import _default_compose_project, resolve_env_with_compose_project

DEFAULT_COMPOSE_PROJECT = _default_compose_project(None)


def test_preserves_explicit_compose_project_name() -> None:
    base = {
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "COMPOSE_PROJECT_NAME": "custom_stack",
    }
    out = resolve_env_with_compose_project("compose.yml", base, dk_env_name="local")
    assert out["COMPOSE_PROJECT_NAME"] == "custom_stack"


def test_local_unix_socket_uses_pas_indmo_prefixed_env_slug() -> None:
    base = {"DOCKER_HOST": "unix:///var/run/docker.sock"}
    out = resolve_env_with_compose_project("compose.yml", base, dk_env_name="local_foo")
    assert out["COMPOSE_PROJECT_NAME"] == "pas_indmo_local_foo"


def test_local_empty_docker_host_uses_prefixed_slug() -> None:
    base: dict[str, str] = {}
    out = resolve_env_with_compose_project("compose.yml", base, dk_env_name="dev")
    assert out["COMPOSE_PROJECT_NAME"] == "pas_indmo_dev"


def test_local_npipe_uses_prefixed_slug() -> None:
    base = {"DOCKER_HOST": "npipe:////./pipe/docker_engine"}
    out = resolve_env_with_compose_project("compose.yml", base, dk_env_name="local")
    assert out["COMPOSE_PROJECT_NAME"] == "pas_indmo_local"


def test_local_tcp_loopback_uses_prefixed_slug() -> None:
    base = {"DOCKER_HOST": "tcp://127.0.0.1:2375"}
    out = resolve_env_with_compose_project("compose.yml", base, dk_env_name="my-env")
    assert out["COMPOSE_PROJECT_NAME"] == "pas_indmo_my-env"


def test_remote_ssh_uses_default_pas_indmo() -> None:
    base = {"DOCKER_HOST": "ssh://root@staging.example.com"}
    out = resolve_env_with_compose_project("compose.yml", base, dk_env_name="staging")
    assert out["COMPOSE_PROJECT_NAME"] == DEFAULT_COMPOSE_PROJECT


@pytest.mark.parametrize(
    "host",
    ("tcp://10.0.0.1:2375", "tcp://192.168.1.5:2376"),
)
def test_remote_tcp_non_loopback_uses_default(host: str) -> None:
    base = {"DOCKER_HOST": host}
    out = resolve_env_with_compose_project("compose.yml", base, dk_env_name="staging")
    assert out["COMPOSE_PROJECT_NAME"] == DEFAULT_COMPOSE_PROJECT


def test_sanitizes_odd_env_name_chars() -> None:
    base = {"DOCKER_HOST": "unix:///var/run/docker.sock"}
    out = resolve_env_with_compose_project("compose.yml", base, dk_env_name="local  weird!!name")
    assert out["COMPOSE_PROJECT_NAME"] == "pas_indmo_local-weird-name"
