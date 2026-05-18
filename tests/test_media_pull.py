"""Tests for media volume pull (docker run + tar)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from catalpa_tooling.media_pull import run_pull_media, run_push_media


def test_run_pull_media_dry_run_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    rc = run_pull_media(
        {"COMPOSE_PROJECT_NAME": "myproj"},
        target=Path("/tmp/foo/media"),
        dry_run=True,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "myproj_django_media" in err
    assert "docker run" in err
    assert "tar" in err and "x" in err and "/out" in err


def test_run_pull_media_pipes_tar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "media"

    class FakePopen:
        def __init__(self, cmd: list[str], **kwargs: object) -> None:
            self.cmd = cmd
            self.stdout = MagicMock()
            self.stdout.close = MagicMock()
            self.stderr = MagicMock()
            self.stderr.read = MagicMock(return_value=b"")

        def wait(self) -> int:
            return 0

    recorded_popen: list[list[str]] = []
    recorded_run: list[list[str]] = []

    def fake_popen(cmd: list[str], **kwargs: object) -> FakePopen:
        recorded_popen.append(cmd)
        return FakePopen(cmd, **kwargs)

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        recorded_run.append(cmd)
        r = MagicMock(spec=["returncode", "stderr"])
        r.returncode = 0
        r.stderr = b""
        return r

    monkeypatch.setattr("catalpa_tooling.media_pull.subprocess.Popen", fake_popen)
    monkeypatch.setattr("catalpa_tooling.media_pull.subprocess.run", fake_run)

    rc = run_pull_media(
        {"COMPOSE_PROJECT_NAME": "pas_indmo"},
        target=target,
        dry_run=False,
        alpine_image="alpine:3.21",
    )
    assert rc == 0
    assert len(recorded_popen) == 1
    assert recorded_popen[0][0] == "docker"
    assert "pas_indmo_django_media:/data:ro" in recorded_popen[0]
    assert recorded_popen[0][-5:] == ["tar", "c", "-C", "/data", "."]
    assert len(recorded_run) == 1
    assert recorded_run[0][0] == "docker"
    assert recorded_run[0][-4:] == ["tar", "x", "-C", "/out"]
    assert target.is_dir()


def test_run_pull_media_docker_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "media"

    class BadPopen:
        def __init__(self, cmd: list[str], **kwargs: object) -> None:
            self.stdout = MagicMock()
            self.stdout.close = MagicMock()
            self.stderr = MagicMock()
            self.stderr.read = MagicMock(return_value=b"no such volume\n")

        def wait(self) -> int:
            return 1

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        r = MagicMock(spec=["returncode", "stderr"])
        r.returncode = 0
        r.stderr = b""
        return r

    monkeypatch.setattr("catalpa_tooling.media_pull.subprocess.Popen", BadPopen)
    monkeypatch.setattr("catalpa_tooling.media_pull.subprocess.run", fake_run)

    rc = run_pull_media(
        {"COMPOSE_PROJECT_NAME": "x"},
        target=target,
        dry_run=False,
    )
    assert rc == 1


def test_run_push_media_dry_run_stderr(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    src = tmp_path / "media"
    src.mkdir()
    rc = run_push_media(
        {"COMPOSE_PROJECT_NAME": "myproj"},
        source=src,
        dry_run=True,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "myproj_django_media" in err
    assert "tar c" in err
    assert "find /data" in err


def test_run_push_media_not_a_directory(tmp_path: Path) -> None:
    f = tmp_path / "notadir"
    f.write_text("x", encoding="utf-8")
    rc = run_push_media(
        {"COMPOSE_PROJECT_NAME": "z"},
        source=f,
        dry_run=False,
    )
    assert rc == 1


def test_run_push_media_pipes_tar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    src = tmp_path / "media"
    src.mkdir()
    recorded: list[list[str]] = []

    class PackPopen:
        def __init__(self, cmd: list[str], **kwargs: object) -> None:
            self.cmd = cmd
            self.stdout = MagicMock()
            self.stdout.close = MagicMock()
            self.stderr = MagicMock()
            self.stderr.read = MagicMock(return_value=b"")

        def wait(self) -> int:
            return 0

    class DockerPopen:
        def __init__(self, cmd: list[str], **kwargs: object) -> None:
            recorded.append(cmd)
            self.stdout = MagicMock()
            self.stderr = MagicMock()
            self.returncode = 0

        def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    def fake_popen(cmd: list[str], **kwargs: object) -> PackPopen | DockerPopen:
        joined = "".join(cmd)
        if "/src:ro" in joined and "tar" in cmd and cmd[-1] == ".":
            return PackPopen(cmd, **kwargs)
        return DockerPopen(cmd, **kwargs)

    monkeypatch.setattr("catalpa_tooling.media_pull.subprocess.Popen", fake_popen)

    rc = run_push_media(
        {"COMPOSE_PROJECT_NAME": "pas_indmo"},
        source=src,
        dry_run=False,
        alpine_image="alpine:3.21",
    )
    assert rc == 0
    assert len(recorded) == 1
    d = recorded[0]
    assert d[0] == "docker"
    assert "pas_indmo_django_media:/data" in d
    assert ":ro" not in "".join(d)
    assert "-i" in d
    assert "sh" in d and "-c" in d
    inner = d[d.index("-c") + 1]
    assert "find /data" in inner
    assert "tar x -C /data" in inner
