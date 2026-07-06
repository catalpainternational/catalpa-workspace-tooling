"""Tests for ``resolve_env_with_compose_project`` (Compose / dk volume prefix behaviour)."""

import pytest

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.restic_files import _default_compose_project, resolve_env_with_compose_project


def test_preserves_explicit_compose_project_name(minimal_config: ProjectConfig) -> None:
    base = {
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "COMPOSE_PROJECT_NAME": "custom_stack",
    }
    out = resolve_env_with_compose_project(
        "compose.yml", base, dk_env_name="local", config=minimal_config
    )
    assert out["COMPOSE_PROJECT_NAME"] == "custom_stack"


def test_local_unix_socket_uses_compose_project_prefixed_env_slug(
    minimal_config: ProjectConfig,
) -> None:
    base = {"DOCKER_HOST": "unix:///var/run/docker.sock"}
    out = resolve_env_with_compose_project(
        "compose.yml", base, dk_env_name="local_foo", config=minimal_config
    )
    assert out["COMPOSE_PROJECT_NAME"] == "app_compose_local_foo"


def test_local_empty_docker_host_uses_prefixed_slug(minimal_config: ProjectConfig) -> None:
    base: dict[str, str] = {}
    out = resolve_env_with_compose_project(
        "compose.yml", base, dk_env_name="dev", config=minimal_config
    )
    assert out["COMPOSE_PROJECT_NAME"] == "app_compose_dev"


def test_local_npipe_uses_prefixed_slug(minimal_config: ProjectConfig) -> None:
    base = {"DOCKER_HOST": "npipe:////./pipe/docker_engine"}
    out = resolve_env_with_compose_project(
        "compose.yml", base, dk_env_name="local", config=minimal_config
    )
    assert out["COMPOSE_PROJECT_NAME"] == "app_compose_local"


def test_local_tcp_loopback_uses_prefixed_slug(minimal_config: ProjectConfig) -> None:
    base = {"DOCKER_HOST": "tcp://127.0.0.1:2375"}
    out = resolve_env_with_compose_project(
        "compose.yml", base, dk_env_name="my-env", config=minimal_config
    )
    assert out["COMPOSE_PROJECT_NAME"] == "app_compose_my-env"


def test_remote_ssh_uses_compose_project_default(minimal_config: ProjectConfig) -> None:
    base = {"DOCKER_HOST": "ssh://root@staging.example.com"}
    out = resolve_env_with_compose_project(
        "compose.yml", base, dk_env_name="staging", config=minimal_config
    )
    assert out["COMPOSE_PROJECT_NAME"] == _default_compose_project(minimal_config)


@pytest.mark.parametrize(
    "host",
    ("tcp://10.0.0.1:2375", "tcp://192.168.1.5:2376"),
)
def test_remote_tcp_non_loopback_uses_default(host: str, minimal_config: ProjectConfig) -> None:
    base = {"DOCKER_HOST": host}
    out = resolve_env_with_compose_project(
        "compose.yml", base, dk_env_name="staging", config=minimal_config
    )
    assert out["COMPOSE_PROJECT_NAME"] == _default_compose_project(minimal_config)


def test_sanitizes_odd_env_name_chars(minimal_config: ProjectConfig) -> None:
    base = {"DOCKER_HOST": "unix:///var/run/docker.sock"}
    out = resolve_env_with_compose_project(
        "compose.yml", base, dk_env_name="local  weird!!name", config=minimal_config
    )
    assert out["COMPOSE_PROJECT_NAME"] == "app_compose_local-weird-name"


def test_requires_config_when_compose_project_unset() -> None:
    with pytest.raises(Exception, match="ProjectConfig is required"):
        resolve_env_with_compose_project("compose.yml", {}, dk_env_name="dev")
