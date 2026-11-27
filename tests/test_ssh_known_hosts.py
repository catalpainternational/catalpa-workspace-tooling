"""Tests for SSH known_hosts registration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from catalpa_tooling.ssh_known_hosts import (
    ensure_known_host,
    ensure_ssh_known_host_for_docker_host,
    ensure_ssh_known_host_for_ssh_target,
    host_and_port_from_ssh_target,
    host_in_known_hosts,
    is_ssh_host_key_verification_error,
    known_hosts_path,
    ssh_host_from_docker_host,
)


def test_ssh_host_from_docker_host_ssh_url() -> None:
    assert ssh_host_from_docker_host("ssh://root@1.2.3.4") == "1.2.3.4"
    assert ssh_host_from_docker_host("ssh://deploy@host.example.com") == "host.example.com"


def test_host_and_port_from_ssh_target() -> None:
    assert host_and_port_from_ssh_target("root@bero.catalpa.io") == ("bero.catalpa.io", 22)
    assert host_and_port_from_ssh_target("deploy@10.0.0.5:2222") == ("10.0.0.5", 2222)
    assert host_and_port_from_ssh_target("host.example") == ("host.example", 22)


def test_ensure_ssh_known_host_for_ssh_target_delegates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kh = tmp_path / "known_hosts"
    called: list[tuple[str, int]] = []

    def fake_ensure(host: str, *, port: int = 22, **kwargs):
        called.append((host, port))
        return 0

    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts.ensure_known_host", fake_ensure)
    assert ensure_ssh_known_host_for_ssh_target("root@bero.catalpa.io", known_hosts=kh) == 0
    assert called == [("bero.catalpa.io", 22)]


def test_is_ssh_host_key_verification_error() -> None:
    assert is_ssh_host_key_verification_error("Host key verification failed.")
    assert is_ssh_host_key_verification_error("No SSH host key is known for bero.catalpa.io")
    assert not is_ssh_host_key_verification_error("connection timed out")


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

    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts._ssh_port_open", lambda *_a, **_k: True)
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

    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts._ssh_port_open", lambda *_a, **_k: True)
    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts.time.sleep", lambda *_a: None)
    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts.run_cmd", fake_run)
    assert (
        ensure_known_host(
            "203.0.113.5",
            known_hosts=kh,
            timeout_seconds=3,
            poll_interval=1,
            recovery_env_name="prod",
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "ssh-keyscan failed" in err
    assert "dk prod host --write" in err


def test_ensure_known_host_retries_until_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kh = tmp_path / "known_hosts"
    scan_attempts = 0

    def fake_run(cmd, **_k):
        nonlocal scan_attempts
        if cmd[0] == "ssh-keygen":
            return MagicMock(returncode=1)
        if cmd[0] == "ssh-keyscan":
            scan_attempts += 1
            if scan_attempts < 2:
                return MagicMock(returncode=1, stdout="", stderr="Broken pipe")
            return MagicMock(
                returncode=0,
                stdout="|1|abc| ssh-ed25519 AAAAB3NzaC1lZDI1NTE5\n",
                stderr="",
            )
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts._ssh_port_open", lambda *_a, **_k: True)
    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts.time.sleep", lambda *_a: None)
    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts.run_cmd", fake_run)
    assert ensure_known_host("203.0.113.5", known_hosts=kh, timeout_seconds=30) == 0
    assert scan_attempts == 2


def test_known_hosts_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom_known_hosts"
    monkeypatch.setenv("SSH_KNOWN_HOSTS", str(custom))
    assert known_hosts_path() == custom


OLD_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEW_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


def _entries(kh: Path, host: str) -> list[str]:
    import subprocess

    r = subprocess.run(["ssh-keygen", "-F", host, "-f", str(kh)], check=False, capture_output=True, text=True)
    return [" ".join(ln.split()[1:3]) for ln in r.stdout.splitlines() if ln and not ln.startswith("#")]


def test_alias_known_host_replaces_an_earlier_hosts_key(tmp_path: Path) -> None:
    """A new droplet behind a name an older host had: the name gets the new droplet's key."""
    from catalpa_tooling.ssh_known_hosts import alias_known_host

    kh = tmp_path / "known_hosts"
    kh.write_text(f"staging.example.com {OLD_KEY}\n203.0.113.7 {NEW_KEY}\nother.example {OLD_KEY}\n")
    assert alias_known_host("staging.example.com", "203.0.113.7", path=kh) == 0
    assert _entries(kh, "staging.example.com") == [NEW_KEY]
    assert _entries(kh, "203.0.113.7") == [NEW_KEY]
    assert _entries(kh, "other.example") == [OLD_KEY]


def test_alias_known_host_adds_a_name_with_no_entry_and_keeps_the_port(tmp_path: Path) -> None:
    from catalpa_tooling.ssh_known_hosts import alias_known_host

    kh = tmp_path / "known_hosts"
    kh.write_text(f"[203.0.113.7]:2222 {NEW_KEY}\n")
    assert alias_known_host("staging.example.com", "203.0.113.7", port=2222, path=kh) == 0
    assert _entries(kh, "[staging.example.com]:2222") == [NEW_KEY]


def test_alias_known_host_without_the_ips_keys_changes_nothing(tmp_path: Path) -> None:
    from catalpa_tooling.ssh_known_hosts import alias_known_host

    kh = tmp_path / "known_hosts"
    kh.write_text(f"staging.example.com {OLD_KEY}\n")
    assert alias_known_host("staging.example.com", "203.0.113.7", path=kh) == 1
    assert _entries(kh, "staging.example.com") == [OLD_KEY]
    assert alias_known_host("203.0.113.7", "203.0.113.7", path=kh) == 0  # docker_host is the IP
