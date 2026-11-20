"""``dk fetch db`` / ``dk fetch media`` from a ``runtime: quadlet`` env (``fcos/fetch.py``)."""

from __future__ import annotations

import io
import shlex
import shutil
from pathlib import Path

import pytest
import yaml

from catalpa_tooling.config import load_project_config
from catalpa_tooling.fcos import fetch

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"


@pytest.fixture
def site(tmp_path: Path, monkeypatch):
    root = tmp_path / "site"
    shutil.copytree(FIXTURES / "site", root)
    shutil.copytree(root / "expected", root / "docker" / "envs" / "staging" / "generated" / "fcos")
    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts.ensure_known_host", lambda *a, **k: 0)
    return load_project_config(root)


def test_only_quadlet_envs_are_taken(site) -> None:
    q = fetch.quadlet_env(site, "staging")
    assert q is not None and q.ssh_target == "deploy@staging.site.example"
    info = site.deploy_envs_dir / "staging" / "info.yaml"
    data = yaml.safe_load(info.read_text())
    data.pop("runtime")
    info.write_text(yaml.safe_dump(data))
    assert fetch.quadlet_env(site, "staging") is None
    assert fetch.quadlet_env(site, "no-such-env") is None


def test_pg_dump_command_needs_no_password() -> None:
    argv = shlex.split(fetch.pg_dump_command("db", ["--exclude-table", "x y"]))
    assert argv[:7] == ["podman", "exec", "-i", "-u", "postgres", "db", "sh"]
    inner = argv[-1]
    assert 'exec pg_dump -U postgres -d "$APP_DB" -Fc' in inner
    assert "PGPASSWORD" not in inner and "-h " not in inner
    assert inner.endswith("--exclude-table 'x y'")


class FakeProc:
    def __init__(self, out: bytes, rc: int = 0, err: bytes = b"") -> None:
        self.stdout, self.stderr, self.rc = io.BytesIO(out), io.BytesIO(err), rc

    def wait(self) -> int:
        return self.rc


def test_fetch_db_writes_a_whole_archive_only(site, tmp_path) -> None:
    q = fetch.quadlet_env(site, "staging")
    dest = tmp_path / "out" / "app.dump"
    seen = []

    def popen(argv, **kw):
        seen.append(argv)
        return FakeProc(b"PGDMP" + b"\0" * 200_000)

    fetch.fetch_db(site, q, dest=dest, popen=popen)
    assert dest.read_bytes()[:5] == b"PGDMP"
    assert seen[0][-2] == "deploy@staging.site.example"

    dest.write_bytes(b"previous dump")
    with pytest.raises(SystemExit):
        fetch.fetch_db(site, q, dest=dest, popen=lambda a, **k: FakeProc(b"", 1, b"no such container"))
    assert dest.read_bytes() == b"previous dump"  # a failed fetch keeps the old dump
    assert not dest.with_name("app.dump.tmp").exists()


def test_media_path(site) -> None:
    q = fetch.quadlet_env(site, "staging")
    assert fetch.media_path(site, q) == "/var/mnt/data/django_media"


def test_fetch_media_rsyncs_the_host_directory(site, tmp_path, monkeypatch) -> None:
    from catalpa_tooling import fetch_media

    calls = []
    monkeypatch.setattr(fetch_media, "rsync_pull_remote_to_local", lambda *a: calls.append(a) or 0)
    monkeypatch.setattr(fetch_media, "_ensure_ssh_target_known", lambda t: None)
    monkeypatch.setattr(
        fetch_media, "docker_volume_mountpoint_ssh", lambda *a, **k: pytest.fail("no Docker volume")
    )
    monkeypatch.setattr(fetch_media.shutil, "which", lambda name: f"/usr/bin/{name}")
    fetch_media.run_fetch_media(
        site, dk_env="staging", host=None, dest=tmp_path / "m", partial=False,
        legacy_path=False, legacy_remote=None, compose_project=None,
    )  # fmt: skip
    assert calls == [("deploy@staging.site.example", "/var/mnt/data/django_media/", (tmp_path / "m").resolve())]
