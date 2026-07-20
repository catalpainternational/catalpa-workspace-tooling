"""Tests for ``dk transfer`` media rsync orchestration."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalpa_tooling.media_storage import MediaStorage, MediaStorageKind
from catalpa_tooling.media_transfer import run_transfer_media


def _vol(name: str = "proj_django_media") -> MediaStorage:
    return MediaStorage(MediaStorageKind.VOLUME, name)


def test_transfer_media_tar_method(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.run_pull_media",
        lambda *a, **k: calls.append("pull") or 0,
    )
    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.run_push_media",
        lambda *a, **k: calls.append("push") or 0,
    )

    rc = run_transfer_media(
        {},
        {},
        src_media=_vol(),
        dst_media=_vol("dst"),
        media_dir=tmp_path / "media",
        method="tar",
    )
    assert rc == 0
    assert calls == ["pull", "push"]


def test_transfer_media_direct_rsync(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/rsync")
    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.resolve_rsync_endpoint",
        lambda env, storage, **k: (
            "/src/mount" if storage.location == "src_vol" else "/dst/mount"
        ),
    )
    recorded: list[tuple[str, str]] = []

    def fake_rsync(src: str, dst: str, **kwargs: object) -> int:
        recorded.append((src, dst))
        return 0

    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.rsync_between_endpoints",
        fake_rsync,
    )

    rc = run_transfer_media(
        {},
        {"DOCKER_HOST": "ssh://u@h"},
        src_media=_vol("src_vol"),
        dst_media=_vol("dst_vol"),
        media_dir=tmp_path / "media",
        method="rsync",
    )
    assert rc == 0
    assert recorded == [("/src/mount", "/dst/mount")]


def test_transfer_media_stage_then_push(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """macOS-style: source not host-reachable, dest is SSH → tar stage + rsync push."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/rsync")

    def fake_resolve(env, storage, **k):
        if storage.location == "dst_vol":
            return "u@h:/vol/dst"
        return None

    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.resolve_rsync_endpoint",
        fake_resolve,
    )
    calls: list[str] = []

    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.run_pull_media",
        lambda *a, **k: calls.append("tar_pull") or 0,
    )
    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.run_push_media_rsync",
        lambda *a, **k: calls.append("rsync_push") or 0,
    )

    rc = run_transfer_media(
        {},
        {"DOCKER_HOST": "ssh://u@h"},
        src_media=_vol("src_vol"),
        dst_media=_vol("dst_vol"),
        media_dir=tmp_path / "media",
        method="rsync",
    )
    assert rc == 0
    assert calls == ["tar_pull", "rsync_push"]


def test_transfer_media_rsync_push_fallback_tar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/rsync")
    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.resolve_rsync_endpoint",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.run_pull_media",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.run_push_media_rsync",
        lambda *a, **k: 7,
    )
    pushed: list[str] = []
    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.run_push_media",
        lambda *a, **k: pushed.append("tar_push") or 0,
    )

    rc = run_transfer_media(
        {},
        {},
        src_media=_vol(),
        dst_media=_vol("dst"),
        media_dir=tmp_path / "media",
        method="rsync",
        fallback_tar=True,
    )
    assert rc == 0
    assert pushed == ["tar_push"]


def test_transfer_media_direct_rsync_fallback_tar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/rsync")
    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.resolve_rsync_endpoint",
        lambda env, storage, **k: f"/{storage.location}",
    )
    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.rsync_between_endpoints",
        lambda *a, **k: 3,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.run_pull_media",
        lambda *a, **k: calls.append("pull") or 0,
    )
    monkeypatch.setattr(
        "catalpa_tooling.media_transfer.run_push_media",
        lambda *a, **k: calls.append("push") or 0,
    )

    rc = run_transfer_media(
        {},
        {},
        src_media=_vol("a"),
        dst_media=_vol("b"),
        media_dir=tmp_path / "media",
        method="rsync",
        fallback_tar=True,
    )
    assert rc == 0
    assert calls == ["pull", "push"]
