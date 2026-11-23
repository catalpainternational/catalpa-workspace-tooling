"""SSH connection reuse for rendered hosts (``ssh_reuse.py``)."""

from __future__ import annotations

import stat

from catalpa_tooling import ssh_reuse
from catalpa_tooling.fcos import host as h


def test_on_by_default_with_a_private_socket_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(ssh_reuse.ENV)
    monkeypatch.setenv("HOME", str(tmp_path))
    argv = h.Host("203.0.113.9")._argv("deploy", "true")
    d = tmp_path / ".ssh" / "dk-control"
    assert argv[:9] == [
        "ssh", "-o", "BatchMode=yes",
        "-o", "ControlMaster=auto", "-o", f"ControlPath={d}/%C", "-o", "ControlPersist=60",
    ]  # fmt: skip
    assert argv[-2:] == ["deploy@203.0.113.9", "true"]
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_dk_ssh_reuse_0_turns_it_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    for value in ("0", "false", "no", "off"):
        monkeypatch.setenv(ssh_reuse.ENV, value)
        assert ssh_reuse.reuse_options() == []
    assert h.Host("x")._argv("deploy", "true") == ["ssh", "-o", "BatchMode=yes", "deploy@x", "true"]
    assert not (tmp_path / ".ssh").exists()


def test_a_directory_it_cant_make_means_no_reuse(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(ssh_reuse.ENV)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".ssh").write_text("not a directory")
    assert ssh_reuse.reuse_options() == []
