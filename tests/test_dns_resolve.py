"""Tests for public DNS resolution helpers."""

from __future__ import annotations

import socket

import pytest

from catalpa_tooling.dns_resolve import (
    docker_host_expected_ipv4,
    resolve_ipv4,
    verify_public_dns,
    verify_public_dns_from_info,
)


def test_resolve_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, port, family, type, proto=0, flags=0):
        assert host == "app.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert resolve_ipv4("app.example.com") == ["203.0.113.5"]


def test_resolve_ipv4_strips_port(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, port, family, type, proto=0, flags=0):
        assert host == "app.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert resolve_ipv4("app.example.com:443") == ["203.0.113.5"]


def test_docker_host_expected_ipv4_literal() -> None:
    assert docker_host_expected_ipv4("ssh://root@203.0.113.5") == "203.0.113.5"


def test_docker_host_expected_ipv4_resolves_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host, port, family, type, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.9", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert docker_host_expected_ipv4("ssh://root@deploy.example.com") == "203.0.113.9"


def test_docker_host_expected_ipv4_rejects_multiple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host, port, family, type, proto=0, flags=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.2", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match="multiple IPv4"):
        docker_host_expected_ipv4("ssh://root@lb.example.com")


def test_verify_public_dns_match(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host, port, family, type, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert verify_public_dns(["staging.example.com"], "203.0.113.5") == 0


def test_verify_public_dns_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    def fake_getaddrinfo(host, port, family, type, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert verify_public_dns(["staging.example.com"], "203.0.113.5") == 1
    assert "Public DNS mismatch" in capsys.readouterr().err


def test_verify_public_dns_from_info_empty() -> None:
    assert verify_public_dns_from_info({}, "203.0.113.5") == 0


def test_verify_public_dns_from_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host, port, family, type, proto=0, flags=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.5", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    info = {"site_origin": ["https://staging.example.com"]}
    assert verify_public_dns_from_info(info, "203.0.113.5") == 0
