"""Tests for media rsync push/pull helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.media_rsync import (
    mountpoint_host_rsync_writable,
    resolve_push_media_source,
    rsync_push_local_to_dest,
    run_push_media_rsync,
    try_ssh_target_from_docker_host,
)
from tests.test_fetch_media_config import _write_minimal_tooling


def test_try_ssh_target_from_unix_socket() -> None:
    assert try_ssh_target_from_docker_host("unix:///var/run/docker.sock") is None


def test_try_ssh_target_from_ssh_url() -> None:
    assert try_ssh_target_from_docker_host("ssh://root@host.example") == "root@host.example"


def test_mountpoint_not_writable_on_darwin() -> None:
    assert mountpoint_host_rsync_writable("/var/lib/docker/volumes/x/_data") is False


def test_resolve_push_media_source_default(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    dest = cfg.fetch_media_dest_path
    dest.mkdir(parents=True)
    (dest / "file.txt").write_text("x", encoding="utf-8")
    got = resolve_push_media_source(cfg, tmp_path, None)
    assert got == dest.resolve()


def test_resolve_push_media_source_missing(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    assert resolve_push_media_source(cfg, tmp_path, None) is None


def test_rsync_push_adds_delete_and_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        recorded.append(list(cmd))
        m = MagicMock()
        m.returncode = 0
        return m

    monkeypatch.setattr("catalpa_tooling.media_rsync.run_cmd", fake_run)
    src = Path("/tmp/media")
    rc = rsync_push_local_to_dest(src, "/vol/mount", delete=True, dry_run=False)
    assert rc == 0
    assert "--delete" in recorded[0]
    assert recorded[0][-2].endswith("/")
    assert recorded[0][-1] == "/vol/mount/"


def test_run_push_media_rsync_ssh_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_tooling: None
) -> None:
    _write_minimal_tooling(tmp_path)
    source = tmp_path / "media"
    source.mkdir()
    (source / "a.txt").write_text("1", encoding="utf-8")
    calls: list[str] = []

    def fake_mount_ssh(ssh: str, vol: str, **_: object) -> str:
        assert ssh == "u@h"
        assert "django_media" in vol
        return "/vol/mount"

    def fake_push(src: Path, dest: str, **kwargs: object) -> int:
        calls.append(dest)
        return 0

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/rsync")
    monkeypatch.setattr(
        "catalpa_tooling.media_rsync.docker_volume_mountpoint_ssh",
        fake_mount_ssh,
    )
    monkeypatch.setattr("catalpa_tooling.media_rsync.rsync_push_local_to_dest", fake_push)

    rc = run_push_media_rsync(
        {"COMPOSE_PROJECT_NAME": "myproj", "DOCKER_HOST": "ssh://u@h"},
        source=source,
        dry_run=False,
    )
    assert rc == 0
    assert calls == ["u@h:/vol/mount/"]


def test_run_push_media_tar_method(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_tooling: None
) -> None:
    source = tmp_path / "media"
    source.mkdir()
    monkeypatch.setattr(
        "catalpa_tooling.media_pull.run_push_media",
        lambda *a, **k: 42,
    )
    rc = run_push_media_rsync(
        {},
        source=source,
        dry_run=False,
        method="tar",
    )
    assert rc == 42
