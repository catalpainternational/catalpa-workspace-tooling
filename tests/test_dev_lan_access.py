"""Tests for dev LAN access detection and env injection."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from catalpa_tooling.dev_lan_access import (
    build_dev_lan_env,
    dev_lan_access_enabled,
    dev_lan_port_from_info,
    format_dev_lan_urls,
    print_dev_lan_urls,
)


def test_dev_lan_access_enabled_when_explicit() -> None:
    assert dev_lan_access_enabled({}) is False
    assert dev_lan_access_enabled({"dev_lan_access": True}) is True


def test_dev_lan_access_disabled_explicitly() -> None:
    assert dev_lan_access_enabled({"dev_lan_access": False}) is False


def test_dev_lan_access_disabled_for_remote() -> None:
    assert dev_lan_access_enabled({"docker_host": "ssh://example", "dev_lan_access": True}) is False


def test_dev_lan_port_from_info() -> None:
    info = {"env": {"node_port": "9001"}}
    assert dev_lan_port_from_info(info) == 9001


def test_dev_lan_port_fallback() -> None:
    assert dev_lan_port_from_info({}) == 8080


@patch("catalpa_tooling.dev_lan_access.detect_dev_lan_hosts", return_value=["192.168.1.42"])
def test_format_dev_lan_urls(mock_detect: object) -> None:
    info = {"dev_lan_access": True, "env": {"node_port": "9001"}}
    assert format_dev_lan_urls(info) == ["http://192.168.1.42:9001"]


@patch("catalpa_tooling.dev_lan_access.detect_dev_lan_hosts", return_value=["192.168.1.42", "Mac.local"])
def test_build_dev_lan_env(mock_detect: object) -> None:
    info = {"dev_lan_access": True, "env": {"node_port": "9001"}}
    env = build_dev_lan_env(info)
    assert env["BERO_EXTRA_ALLOWED_HOSTS"] == "192.168.1.42,Mac.local"
    assert env["BERO_EXTRA_ORIGINS"] == (
        "http://192.168.1.42:9001,http://Mac.local:9001"
    )


def test_build_dev_lan_env_disabled() -> None:
    assert build_dev_lan_env({"dev_lan_access": False}) == {}


@patch("catalpa_tooling.dev_lan_access.detect_dev_lan_hosts", return_value=[])
def test_build_dev_lan_env_no_hosts(mock_detect: object) -> None:
    assert build_dev_lan_env({"dev_lan_access": True}) == {}


def test_print_dev_lan_urls(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.dev_lan_access.detect_dev_lan_hosts",
        lambda: ["10.0.0.5"],
    )
    info = {"dev_lan_access": True, "env": {"node_port": "9001"}}
    urls = print_dev_lan_urls(info)
    assert urls == ["http://10.0.0.5:9001"]
    err = capsys.readouterr().err
    assert "LAN dev URLs:" in err
    assert "http://10.0.0.5:9001" in err
