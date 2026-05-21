"""Tests for catalpa_tooling.trust_caddy_cert."""

from __future__ import annotations

import subprocess
import pytest

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.trust_caddy_cert import (
    CADDY_LOCAL_CA_PATH,
    _proxy_container_id,
    trust_caddy_local_ca,
)


def test_proxy_container_id_requests_captured_compose_output(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_compose(*_args, **kwargs) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess([], 0, stdout="cid\n")

    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert._compose", fake_compose)
    assert _proxy_container_id("compose.yml", "proxy", {}) == "cid"
    assert captured.get("capture_output") is True


def test_non_macos_returns_error(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "linux")
    code = trust_caddy_local_ca("compose.yml", {}, minimal_config)
    assert code == 1
    assert "macOS only" in capsys.readouterr().err


def test_dry_run_macos(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "darwin")

    def fake_compose(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="abc123\n")

    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert._compose", fake_compose)
    env_add = {"COMPOSE_PROJECT_NAME": "app_compose"}
    code = trust_caddy_local_ca(
        "compose.dev.yaml",
        env_add,
        minimal_config,
        dry_run=True,
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "compose.dev.yaml" in err
    assert "proxy" in err
    assert "app_compose" in err
    assert "abc123" in err
    assert "security add-trusted-cert" in err


def test_missing_container(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "darwin")

    def empty_compose(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="")

    def empty_ps(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="")

    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert._compose", empty_compose)
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.run_cmd", empty_ps)

    code = trust_caddy_local_ca("compose.yml", {}, minimal_config)
    assert code == 1
    err = capsys.readouterr().err
    assert "proxy" in err
    assert "not running" in err


def test_happy_path_trusts_cert(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "darwin")
    calls: list[list[str]] = []

    def fake_compose(
        compose_file: str,
        *args: str,
        **_kwargs,
    ) -> subprocess.CompletedProcess[str]:
        assert compose_file == "compose.yml"
        assert args == ("ps", "-q", "proxy")
        return subprocess.CompletedProcess([], 0, stdout="container1\n")

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert._compose", fake_compose)
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.run_cmd", fake_run)

    code = trust_caddy_local_ca("compose.yml", {"COMPOSE_PROJECT_NAME": "x"}, minimal_config)
    assert code == 0
    assert len(calls) == 2
    assert calls[0][:2] == ["docker", "cp"]
    assert calls[0][2].startswith("container1:")
    assert CADDY_LOCAL_CA_PATH in calls[0][2]
    assert calls[1][0] == "sudo"
    assert calls[1][1] == "security"
    assert "add-trusted-cert" in calls[1]


def test_docker_cp_failure(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "darwin")

    monkeypatch.setattr(
        "catalpa_tooling.trust_caddy_cert._compose",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="cid\n"),
    )

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "docker":
            return subprocess.CompletedProcess(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.run_cmd", fake_run)
    assert trust_caddy_local_ca("compose.yml", {}, minimal_config) == 1
