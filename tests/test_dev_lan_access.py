"""Tests for dev LAN access detection and env injection."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from catalpa_tooling.dev_lan_access import (
    build_dev_lan_env,
    build_proxy_lan_env,
    dev_lan_access_enabled,
    dev_lan_port_from_info,
    format_dev_lan_urls,
    format_proxy_lan_urls,
    ip_to_dns_label,
    lan_access_enabled,
    lan_hostname_for,
    print_dev_lan_urls,
)

_PROXY_INFO = {
    "site_origin": "https://ambulancia-dev.localdev.temp.build",
    "local_proxy": {
        "lan_access": True,
    },
}


def test_dev_lan_access_enabled_when_explicit() -> None:
    assert dev_lan_access_enabled({}) is False
    assert dev_lan_access_enabled({"dev_lan_access": True}) is True


def test_lan_access_requires_local_proxy() -> None:
    assert lan_access_enabled({"dev_lan_access": True, "local_proxy": False}) is False
    assert lan_access_enabled(_PROXY_INFO) is True


def test_dev_lan_access_disabled_explicitly() -> None:
    assert dev_lan_access_enabled({"dev_lan_access": False}) is False


def test_dev_lan_access_disabled_for_remote() -> None:
    assert dev_lan_access_enabled({"docker_host": "ssh://example", "dev_lan_access": True}) is False


def test_ip_to_dns_label() -> None:
    assert ip_to_dns_label("192.168.1.42") == "192-168-1-42"


def test_lan_hostname_for_site_origin() -> None:
    host = lan_hostname_for("ambulancia-dev.localdev.temp.build", "192.168.1.42")
    assert host == "ambulancia-dev.192-168-1-42.sslip.io"


def test_lan_hostname_for_custom_suffix() -> None:
    host = lan_hostname_for(
        "ambulancia-dev.localdev.temp.build",
        "10.0.0.5",
        lan_dns_suffix="lan.localdev.temp.build",
    )
    assert host == "ambulancia-dev.10-0-0-5.lan.localdev.temp.build"


def test_dev_lan_port_from_info() -> None:
    info = {"env": {"node_port": "9001"}}
    assert dev_lan_port_from_info(info) == 9001


def test_dev_lan_port_from_local_proxy() -> None:
    assert dev_lan_port_from_info(_PROXY_INFO) == 8080


def test_dev_lan_port_fallback() -> None:
    assert dev_lan_port_from_info({}) == 8080


@patch("catalpa_tooling.dev_lan_access.detect_dev_lan_hosts", return_value=["192.168.1.42"])
def test_format_dev_lan_urls_legacy(mock_detect: object) -> None:
    info = {"dev_lan_access": True, "local_proxy": False, "env": {"node_port": "9001"}}
    assert format_dev_lan_urls(info) == ["http://192.168.1.42:9001"]


@patch("catalpa_tooling.dev_lan_access.detect_dev_lan_ipv4", return_value=["192.168.1.42"])
def test_format_proxy_lan_urls(mock_ipv4: object) -> None:
    urls = format_proxy_lan_urls(_PROXY_INFO)
    assert urls == ["https://ambulancia-dev.192-168-1-42.sslip.io"]


@patch("catalpa_tooling.dev_lan_access.detect_dev_lan_hosts", return_value=["192.168.1.42", "Mac.local"])
def test_build_dev_lan_env_legacy(mock_detect: object) -> None:
    info = {"dev_lan_access": True, "local_proxy": False, "env": {"node_port": "9001"}}
    env = build_dev_lan_env(info)
    assert env["BERO_EXTRA_ALLOWED_HOSTS"] == "192.168.1.42,Mac.local"
    assert env["BERO_EXTRA_ORIGINS"] == (
        "http://192.168.1.42:9001,http://Mac.local:9001"
    )


@patch("catalpa_tooling.dev_lan_access.detect_dev_lan_ipv4", return_value=["192.168.1.42"])
def test_build_proxy_lan_env(mock_ipv4: object) -> None:
    env = build_proxy_lan_env(_PROXY_INFO)
    assert "https://ambulancia-dev.192-168-1-42.sslip.io" in env["DOMAIN"]
    assert env["VITE_EXTRA_ALLOWED_HOSTS"] == ".sslip.io"


def test_build_dev_lan_env_disabled() -> None:
    assert build_dev_lan_env({"dev_lan_access": False}) == {}


@patch("catalpa_tooling.dev_lan_access.detect_dev_lan_hosts", return_value=[])
def test_build_dev_lan_env_no_hosts(mock_detect: object) -> None:
    assert build_dev_lan_env({"dev_lan_access": True}) == {}


def test_print_dev_lan_urls_legacy(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.dev_lan_access.detect_dev_lan_hosts",
        lambda: ["10.0.0.5"],
    )
    info = {"dev_lan_access": True, "local_proxy": False, "env": {"node_port": "9001"}}
    urls = print_dev_lan_urls(info)
    assert urls == ["http://10.0.0.5:9001"]
    err = capsys.readouterr().err
    assert "LAN dev URLs:" in err
    assert "http://10.0.0.5:9001" in err


def test_print_dev_lan_urls_proxy(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.dev_lan_access.detect_dev_lan_ipv4",
        lambda: ["10.0.0.5"],
    )
    urls = print_dev_lan_urls(_PROXY_INFO)
    assert urls == ["https://ambulancia-dev.10-0-0-5.sslip.io"]
    err = capsys.readouterr().err
    assert "dk proxy ca" in err
    assert "https://ambulancia-dev.10-0-0-5.sslip.io" in err
