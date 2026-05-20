"""Tests for SSH known_hosts registration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from catalpa_tooling.ssh_known_hosts import (
    ensure_known_host,
    ensure_ssh_known_host_for_docker_host,
    host_in_known_hosts,
    known_hosts_path,
    ssh_host_from_docker_host,
)


def test_ssh_host_from_docker_host_ssh_url() -> None:
    assert ssh_host_from_docker_host("ssh://root@1.2.3.4") == "1.2.3.4"
    assert ssh_host_from_docker_host("ssh://deploy@host.example.com") == "host.example.com"


def test_ssh_host_from_docker_host_user_at_host() -> None:
    assert ssh_host_from_docker_host("deploy@10.0.0.5") == "10.0.0.5"


def test_ssh_host_from_docker_host_non_ssh() -> None:
    assert ssh_host_from_docker_host("unix:///var/run/docker.sock") is None
    assert ssh_host_from_docker_host("") is None
    assert ssh_host_from_docker_host("tcp://127.0.0.1:2375") is None


def test_ensure_ssh_known_host_for_docker_host_noop_non_ssh() -> None:
    assert ensure_ssh_known_host_for_docker_host("unix:///var/run/docker.sock") == 0


def test_host_in_known_hosts_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kh = tmp_path / "known_hosts"
    monkeypatch.setattr(
        "catalpa_tooling.ssh_known_hosts.run_cmd",
        lambda *_a, **_k: MagicMock(returncode=1),
    )
    assert host_in_known_hosts("1.2.3.4", kh) is False


def test_host_in_known_hosts_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kh = tmp_path / "known_hosts"
    monkeypatch.setattr(
        "catalpa_tooling.ssh_known_hosts.run_cmd",
        lambda *_a, **_k: MagicMock(returncode=0),
    )
    assert host_in_known_hosts("1.2.3.4", kh) is True


def test_ensure_known_host_skips_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kh = tmp_path / "known_hosts"
    monkeypatch.setattr(
        "catalpa_tooling.ssh_known_hosts.host_in_known_hosts",
        lambda *_a, **_k: True,
    )
    scan_called = False

    def fake_run(*_a, **_k):
        nonlocal scan_called
        scan_called = True
        return MagicMock(returncode=0)

    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts.run_cmd", fake_run)
    assert ensure_known_host("1.2.3.4", known_hosts=kh) == 0
    assert scan_called is False


def test_ensure_known_host_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    kh = tmp_path / "known_hosts"
    monkeypatch.setattr(
        "catalpa_tooling.ssh_known_hosts.host_in_known_hosts",
        lambda *_a, **_k: False,
    )
    assert ensure_known_host("1.2.3.4", dry_run=True, known_hosts=kh) == 0
    assert not kh.exists()
    err = capsys.readouterr().err
    assert "dry-run" in err
    assert "1.2.3.4" in err


def test_ensure_known_host_appends_scan_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    kh = tmp_path / "known_hosts"

    def fake_run(cmd, **_k):
        if cmd[0] == "ssh-keygen":
            return MagicMock(returncode=1)
        if cmd[0] == "ssh-keyscan":
            return MagicMock(
                returncode=0,
                stdout="|1|abc| ssh-ed25519 AAAAB3NzaC1lZDI1NTE5\n",
                stderr="",
            )
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts.run_cmd", fake_run)
    assert ensure_known_host("203.0.113.5", known_hosts=kh) == 0
    assert kh.is_file()
    assert "ssh-ed25519" in kh.read_text(encoding="utf-8")
    assert "Registered SSH host key" in capsys.readouterr().err


def test_ensure_known_host_scan_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    kh = tmp_path / "known_hosts"

    def fake_run(cmd, **_k):
        if cmd[0] == "ssh-keygen":
            return MagicMock(returncode=1)
        if cmd[0] == "ssh-keyscan":
            return MagicMock(returncode=1, stdout="", stderr="connection refused")
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts.run_cmd", fake_run)
    assert ensure_known_host("203.0.113.5", known_hosts=kh) == 1
    assert "ssh-keyscan failed" in capsys.readouterr().err


def test_known_hosts_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom_known_hosts"
    monkeypatch.setenv("SSH_KNOWN_HOSTS", str(custom))
    assert known_hosts_path() == custom
