"""#138: dk sets ``STACK_PLATFORM`` so compose can use ``platform: ${STACK_PLATFORM-linux/amd64}``.

Local envs that build from source run natively (``""``); ``dk push``, remote envs and local envs
that pull pinned registry images get ``images.yaml`` ``deploy_platform`` (default amd64). An
``info.yaml`` ``env:`` value always wins.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from catalpa_tooling.build_push import verify_built_image_platforms
from catalpa_tooling.dk_stack import env_for_stack_build
from catalpa_tooling.images import deploy_platform_from_config
from catalpa_tooling.managed_deploy_env import (
    load_managed_deploy_context,
    stack_platform_for_env,
)


def test_deploy_platform_defaults_to_amd64() -> None:
    assert deploy_platform_from_config({}) == "linux/amd64"
    assert deploy_platform_from_config({"deploy_platform": " "}) == "linux/amd64"
    assert deploy_platform_from_config({"deploy_platform": "linux/arm64"}) == "linux/arm64"


@pytest.mark.parametrize(
    ("docker_host", "prepulled", "expected"),
    [
        ("", False, ""),
        ("unix:///var/run/docker.sock", False, ""),
        ("tcp://localhost:2375", False, ""),
        ("", True, "linux/arm64"),
        ("ssh://deploy@203.0.113.5", False, "linux/arm64"),
        ("tcp://203.0.113.5:2376", False, "linux/arm64"),
    ],
)
def test_stack_platform_for_env(docker_host: str, prepulled: bool, expected: str) -> None:
    images = {"deploy_platform": "linux/arm64"}
    assert (
        stack_platform_for_env(images, docker_host=docker_host, use_prepulled_registry=prepulled)
        == expected
    )


def test_local_build_is_native_unless_project_env_sets_it(minimal_project) -> None:
    assert env_for_stack_build(minimal_project)["STACK_PLATFORM"] == ""

    (minimal_project.repo_root / "minimal.env").write_text(
        "STACK_PLATFORM=linux/amd64\n", encoding="utf-8"
    )
    assert env_for_stack_build(minimal_project)["STACK_PLATFORM"] == "linux/amd64"


def test_push_platform_overrides_project_env(minimal_project) -> None:
    (minimal_project.repo_root / "minimal.env").write_text("STACK_PLATFORM=\n", encoding="utf-8")

    env = env_for_stack_build(minimal_project, stack_platform="linux/amd64")
    assert env["STACK_PLATFORM"] == "linux/amd64"


def _write_env(config, name: str, body: str) -> None:
    env_dir = config.deploy_envs_dir / name
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "info.yaml").write_text(
        f"name: {name}\nsite_origin: web.example.com\ncredentials_decrypt_optional: true\n{body}",
        encoding="utf-8",
    )


def test_local_env_context_runs_natively(minimal_project) -> None:
    _write_env(minimal_project, "local", "")
    ctx = load_managed_deploy_context(minimal_project, "local")
    assert ctx is not None
    assert ctx.env_add["STACK_PLATFORM"] == ""


def test_info_yaml_env_value_wins(minimal_project) -> None:
    _write_env(minimal_project, "local", "env:\n  STACK_PLATFORM: linux/amd64\n")
    ctx = load_managed_deploy_context(minimal_project, "local")
    assert ctx is not None
    assert ctx.env_add["STACK_PLATFORM"] == "linux/amd64"


def test_remote_env_context_uses_deploy_platform(minimal_project) -> None:
    _write_env(minimal_project, "local", "docker_host: ssh://deploy@203.0.113.5\n")
    with patch(
        "catalpa_tooling.ssh_known_hosts.ensure_ssh_known_host_for_docker_host",
        return_value=0,
    ):
        ctx = load_managed_deploy_context(minimal_project, "local")
    assert ctx is not None
    assert ctx.env_add["STACK_PLATFORM"] == "linux/amd64"


class TestVerifyAgainstDeployPlatform:
    @staticmethod
    def _run(services: dict, platforms: list[str], deploy_platform: str) -> int:
        inspects = [MagicMock(returncode=0, stdout=f"{p}\n", stderr="") for p in platforms]
        config = MagicMock()
        config.compose_prod = "compose.yml"
        with (
            patch(
                "catalpa_tooling.stale_stack._compose_config",
                return_value={"services": services},
            ),
            patch("catalpa_tooling.build_push.run_cmd", side_effect=inspects),
        ):
            return verify_built_image_platforms(config, {}, deploy_platform=deploy_platform)

    def test_unpinned_service_built_native_blocks_the_push(self) -> None:
        services = {"web": {"image": "ghcr.io/x/web:t", "build": {"context": "."}}}
        assert self._run(services, ["linux/arm64"], "linux/amd64") == 1

    def test_unpinned_service_built_for_deploy_platform_passes(self) -> None:
        services = {"web": {"image": "ghcr.io/x/web:t", "build": {"context": "."}}}
        assert self._run(services, ["linux/amd64"], "linux/amd64") == 0

    def test_service_pinned_to_another_platform_blocks(self) -> None:
        services = {
            "web": {"image": "ghcr.io/x/web:t", "build": {"context": "."}, "platform": "linux/arm64"}
        }
        assert self._run(services, ["linux/arm64"], "linux/amd64") == 1

    def test_interpolated_pin_matching_deploy_platform_passes(self) -> None:
        services = {
            "web": {"image": "ghcr.io/x/web:t", "build": {"context": "."}, "platform": "linux/amd64"}
        }
        assert self._run(services, ["linux/amd64"], "linux/amd64") == 0
