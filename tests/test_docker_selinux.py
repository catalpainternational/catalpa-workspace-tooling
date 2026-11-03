"""SELinux opt-out for dk's own bind-mounting ``docker run`` helpers (#128)."""

from __future__ import annotations

import subprocess
import pytest

from catalpa_tooling import docker_selinux, local_proxy, media_pull
from catalpa_tooling.media_storage import MediaStorage, MediaStorageKind

LABEL = ["--security-opt", "label=disable"]
# Captured before conftest stubs it out for every test.
_real_engine_reports_selinux = docker_selinux.engine_reports_selinux


@pytest.fixture
def selinux_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker_selinux, "engine_reports_selinux", lambda env=None: True)
    monkeypatch.setattr(local_proxy, "engine_reports_selinux", lambda env=None: True)


def _fake_docker_info(monkeypatch: pytest.MonkeyPatch, stdout: str, rc: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout)

    monkeypatch.setattr(docker_selinux, "run_cmd", fake_run)
    monkeypatch.setattr(docker_selinux, "_engine_cache", {})
    return calls


def test_engine_reports_selinux_reads_security_options(monkeypatch) -> None:
    calls = _fake_docker_info(
        monkeypatch, '["name=seccomp,profile=default","name=rootless","name=selinux"]\n'
    )
    assert _real_engine_reports_selinux({"DOCKER_HOST": "unix:///run/podman.sock"})
    # cached per engine
    assert _real_engine_reports_selinux({"DOCKER_HOST": "unix:///run/podman.sock"})
    assert len(calls) == 1
    assert calls[0][:2] == ["docker", "info"]


@pytest.mark.parametrize(
    "stdout,rc",
    [('["name=seccomp,profile=builtin","name=cgroupns"]', 0), ("", 1)],
)
def test_engine_reports_selinux_is_false_without_it_or_on_error(monkeypatch, stdout, rc) -> None:
    _fake_docker_info(monkeypatch, stdout, rc)
    assert _real_engine_reports_selinux({}) is False


def test_label_args_empty_without_selinux() -> None:
    assert docker_selinux.selinux_label_disable_args({}) == []


def test_label_args_when_engine_has_selinux(selinux_on) -> None:
    assert docker_selinux.selinux_label_disable_args({}) == LABEL


@pytest.mark.parametrize("value,expected", [("1", LABEL), ("yes", LABEL), ("0", []), ("off", [])])
def test_env_override_beats_detection(monkeypatch, value, expected) -> None:
    monkeypatch.setattr(docker_selinux, "engine_reports_selinux", lambda env=None: not expected)
    assert docker_selinux.selinux_label_disable_args({"DK_SELINUX_LABEL_DISABLE": value}) == expected


# --- local proxy -------------------------------------------------------------------------------


def _proxy_run_stub(monkeypatch, tmp_path, *, exists: bool, security_opt: str = "null",
                    state: str = "true 0", logs: str = ""):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["docker", "inspect"]:
            fmt = cmd[-1]
            if "SecurityOpt" in fmt:
                return subprocess.CompletedProcess(cmd, 0, stdout=security_opt + "\n")
            if "State" in fmt:
                return subprocess.CompletedProcess(cmd, 0, stdout=state + "\n")
            return subprocess.CompletedProcess(cmd, 0, stdout="[]")
        if cmd[:2] == ["docker", "logs"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=logs, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(local_proxy, "run_cmd", fake_run)
    monkeypatch.setattr(local_proxy, "require_local_docker_endpoint", lambda **_: 0)
    monkeypatch.setattr(local_proxy, "ensure_proxy_network", lambda **_: 0)
    monkeypatch.setattr(local_proxy, "ensure_proxy_on_network", lambda **_: 0)
    monkeypatch.setattr(local_proxy, "proxy_container_id", lambda: "")
    monkeypatch.setattr(local_proxy, "proxy_container_exists", lambda: exists)
    monkeypatch.setattr(local_proxy, "local_proxy_data_dir", lambda: tmp_path / "ca")
    live = tmp_path / "Caddyfile"
    live.write_text("# bundled\n", encoding="utf-8")
    monkeypatch.setattr(local_proxy, "local_proxy_caddyfile_path", lambda: live)
    return calls


def _runs(calls):
    return [c for c in calls if c[:2] == ["docker", "run"]]


def test_proxy_run_unchanged_without_selinux(monkeypatch, tmp_path) -> None:
    calls = _proxy_run_stub(monkeypatch, tmp_path, exists=False)
    assert local_proxy.ensure_proxy_running() == 0
    assert "--security-opt" not in _runs(calls)[0]


def test_proxy_run_disables_labels_under_selinux(monkeypatch, tmp_path, selinux_on) -> None:
    calls = _proxy_run_stub(monkeypatch, tmp_path, exists=False)
    assert local_proxy.ensure_proxy_running() == 0
    run = _runs(calls)[0]
    i = run.index("--security-opt")
    assert run[i + 1] == "label=disable"
    assert i < run.index(local_proxy.CADDY_IMAGE)


def test_existing_unlabelled_proxy_is_recreated_under_selinux(
    monkeypatch, tmp_path, selinux_on, capsys
) -> None:
    calls = _proxy_run_stub(monkeypatch, tmp_path, exists=True, security_opt="null")
    assert local_proxy.ensure_proxy_running() == 0
    assert ["docker", "rm", "-f", local_proxy.LOCAL_PROXY_CONTAINER] in calls
    assert not any(c[:2] == ["docker", "start"] for c in calls)
    assert "label=disable" in _runs(calls)[0]
    assert "SELinux" in capsys.readouterr().err


def test_existing_labelled_proxy_is_just_started(monkeypatch, tmp_path, selinux_on) -> None:
    calls = _proxy_run_stub(monkeypatch, tmp_path, exists=True, security_opt='["label=disable"]')
    assert local_proxy.ensure_proxy_running() == 0
    assert any(c[:2] == ["docker", "start"] for c in calls)
    assert not _runs(calls)


def test_proxy_that_exits_straight_away_is_reported(monkeypatch, tmp_path, capsys) -> None:
    calls = _proxy_run_stub(monkeypatch, tmp_path, exists=False, state="false 1")
    assert local_proxy.ensure_proxy_running() == 1
    err = capsys.readouterr().err
    assert "exit code 1" in err
    assert "is empty" in err
    assert any(c[:2] == ["docker", "logs"] for c in calls)


def test_proxy_exit_under_selinux_points_at_the_fix(monkeypatch, tmp_path, selinux_on, capsys) -> None:
    _proxy_run_stub(monkeypatch, tmp_path, exists=False, state="false 1", logs="Error: open Caddyfile: permission denied\n")
    assert local_proxy.ensure_proxy_running() == 1
    err = capsys.readouterr().err
    assert "permission denied" in err
    assert "PODMAN.md" in err


def test_restarted_proxy_that_exits_is_reported(monkeypatch, tmp_path, capsys) -> None:
    _proxy_run_stub(monkeypatch, tmp_path, exists=True, state="false 1")
    monkeypatch.setattr(local_proxy, "proxy_caddyfile_mount_is_stale", lambda: False)
    assert local_proxy.ensure_proxy_running() == 1
    assert "exited straight after starting" in capsys.readouterr().err


def test_uninspectable_proxy_is_not_treated_as_exited(monkeypatch, tmp_path) -> None:
    _proxy_run_stub(monkeypatch, tmp_path, exists=False, state="")
    assert local_proxy.ensure_proxy_running() == 0


# --- media helpers -----------------------------------------------------------------------------


def test_media_pull_labels_bind_and_extract_but_not_volumes(monkeypatch, tmp_path, selinux_on, capsys) -> None:
    bind = MediaStorage(MediaStorageKind.BIND, str(tmp_path / "media"))
    assert media_pull.run_pull_media({}, target=tmp_path / "out", dry_run=True, storage=bind) == 0
    dry = capsys.readouterr().err
    source_cmd, extract_cmd = dry.split("dry-run: ", 2)[2].split(" | ")
    assert "--security-opt label=disable" in source_cmd
    assert "--security-opt label=disable" in extract_cmd

    vol = MediaStorage(MediaStorageKind.VOLUME, "proj_media")
    assert media_pull.run_pull_media({}, target=tmp_path / "out", dry_run=True, storage=vol) == 0
    source_cmd, extract_cmd = capsys.readouterr().err.split("dry-run: ", 2)[2].split(" | ")
    assert "--security-opt" not in source_cmd
    assert "--security-opt label=disable" in extract_cmd


def test_media_push_labels_pack_and_bind(monkeypatch, tmp_path, selinux_on, capsys) -> None:
    src = tmp_path / "src"
    src.mkdir()
    bind = MediaStorage(MediaStorageKind.BIND, str(tmp_path / "media"))
    assert media_pull.run_push_media({}, source=src, dry_run=True, storage=bind) == 0
    pack_cmd, docker_cmd = capsys.readouterr().err.split("dry-run: ", 2)[2].split(" | ")
    assert "--security-opt label=disable" in pack_cmd
    assert "--security-opt label=disable" in docker_cmd


def test_media_pull_unchanged_without_selinux(tmp_path, capsys) -> None:
    bind = MediaStorage(MediaStorageKind.BIND, str(tmp_path / "media"))
    assert media_pull.run_pull_media({}, target=tmp_path / "out", dry_run=True, storage=bind) == 0
    assert "--security-opt" not in capsys.readouterr().err
