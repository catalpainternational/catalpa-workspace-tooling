"""Tests for catalpa_tooling.trust_caddy_cert."""

from __future__ import annotations

import subprocess
import pytest

from pathlib import Path

from catalpa_tooling import trust_caddy_cert
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.trust_caddy_cert import (
    CADDY_LOCAL_CA_PATH,
    _compose_proxy_container_id,
    resolve_trust_caddy_container,
    trust_caddy_ca_from_container,
    trust_caddy_local_ca,
)



@pytest.fixture(autouse=True)
def _trust_store_folders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Both families' folders exist, as on the machine each test pretends to be."""
    debian = tmp_path / "usr/local/share/ca-certificates"
    fedora = tmp_path / "etc/pki/ca-trust/source/anchors"
    debian.mkdir(parents=True)
    fedora.mkdir(parents=True)
    monkeypatch.setattr(
        trust_caddy_cert,
        "_LINUX_TRUST_STORES",
        (("update-ca-certificates", debian), ("update-ca-trust", fedora)),
    )


def test_compose_proxy_container_id_requests_captured_compose_output(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_compose(*_args, **kwargs) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess([], 0, stdout="cid\n")

    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert._compose", fake_compose)
    assert _compose_proxy_container_id("compose.yml", "proxy", {}) == "cid"
    assert captured.get("capture_output") is True


def test_resolve_trust_prefers_local_proxy_container(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.trust_caddy_cert.proxy_container_id",
        lambda: "global-proxy",
    )

    def fail_compose(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        raise AssertionError("compose should not be queried when local proxy is enabled")

    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert._compose", fail_compose)
    info = {
        "local_proxy": {"enabled": True, "upstream_port": 5555},
        "site_origin": "https://app-dev.localdev.temp.build",
    }
    assert (
        resolve_trust_caddy_container("compose.yml", {}, minimal_config, info)
        == "global-proxy"
    )


def test_unsupported_platform_returns_error(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "freebsd")
    code = trust_caddy_ca_from_container("cid", dry_run=False)
    assert code == 1


def test_dry_run_macos(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "darwin")

    code = trust_caddy_ca_from_container("abc123", dry_run=True)
    assert code == 0
    err = capsys.readouterr().err
    assert "abc123" in err
    assert CADDY_LOCAL_CA_PATH in err


def test_missing_container(
    minimal_config: ProjectConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = trust_caddy_ca_from_container("", dry_run=False)
    assert code == 1
    assert "no running Caddy container" in capsys.readouterr().err


def test_happy_path_trusts_cert_macos(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "darwin")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.run_cmd", fake_run)

    code = trust_caddy_ca_from_container("container1", dry_run=False)
    assert code == 0
    assert len(calls) == 2
    assert calls[0][:2] == ["docker", "cp"]
    assert calls[0][2].startswith("container1:")
    assert CADDY_LOCAL_CA_PATH in calls[0][2]
    assert calls[1][0] == "sudo"
    assert calls[1][1] == "security"
    assert "add-trusted-cert" in calls[1]


def test_happy_path_trusts_cert_linux(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "linux")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.run_cmd", fake_run)
    monkeypatch.setattr(
        "catalpa_tooling.trust_caddy_cert.shutil.which",
        lambda cmd: "/usr/sbin/update-ca-certificates" if cmd == "update-ca-certificates" else None,
    )

    code = trust_caddy_ca_from_container("container1", dry_run=False)
    assert code == 0
    assert calls[1][:4] == ["sudo", "install", "-m", "0644"]
    assert calls[1][-1].endswith("/usr/local/share/ca-certificates/catalpa-local-proxy-root.crt")
    assert calls[2] == ["sudo", "update-ca-certificates"]


def test_fedora_uses_p11_kit_anchors_and_update_ca_trust(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fedora, RHEL, Silverblue, Bluefin: no update-ca-certificates, a different folder."""
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "linux")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.run_cmd", fake_run)
    monkeypatch.setattr(
        "catalpa_tooling.trust_caddy_cert.shutil.which",
        lambda cmd: "/usr/bin/update-ca-trust" if cmd == "update-ca-trust" else None,
    )

    assert trust_caddy_ca_from_container("container1", dry_run=False) == 0
    assert calls[1][-1].endswith("/etc/pki/ca-trust/source/anchors/catalpa-local-proxy-root.crt")
    assert calls[2] == ["sudo", "update-ca-trust"]


def test_a_linux_with_no_known_trust_store_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "linux")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.run_cmd", fake_run)
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.shutil.which", lambda cmd: None)

    assert trust_caddy_ca_from_container("container1", dry_run=False) == 1
    assert not any(c[0] == "sudo" for c in calls)
    assert "has neither update-ca-certificates with " in capsys.readouterr().err


def test_trust_caddy_local_ca_uses_stack_proxy_when_local_proxy_disabled(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.trust_caddy_cert.trust_caddy_ca_from_container",
        lambda cid, dry_run=False: 0 if cid == "stack-cid" else 1,
    )
    monkeypatch.setattr(
        "catalpa_tooling.trust_caddy_cert.resolve_trust_caddy_container",
        lambda *_a, **_k: "stack-cid",
    )
    assert trust_caddy_local_ca("compose.yml", {}, minimal_config, info={}) == 0


def test_docker_cp_failure(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "darwin")

    def fake_run(cmd: list[str], **_kwargs) -> subprocess.CompletedProcess[str]:
        if cmd[0] == "docker":
            return subprocess.CompletedProcess(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.run_cmd", fake_run)
    assert trust_caddy_ca_from_container("cid", dry_run=False) == 1


def test_a_command_without_its_folder_is_not_that_family(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Arch has update-ca-trust but no Fedora anchors folder: refuse rather than fail in sudo."""
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.sys.platform", "linux")
    monkeypatch.setattr(
        trust_caddy_cert,
        "_LINUX_TRUST_STORES",
        (("update-ca-trust", tmp_path / "no-such-anchors"),),
    )
    monkeypatch.setattr("catalpa_tooling.trust_caddy_cert.shutil.which", lambda c: f"/usr/bin/{c}")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "catalpa_tooling.trust_caddy_cert.run_cmd",
        lambda cmd, **_k: calls.append(list(cmd)) or subprocess.CompletedProcess(cmd, 0),
    )
    assert trust_caddy_ca_from_container("container1", dry_run=False) == 1
    assert not any(c[0] == "sudo" for c in calls)
