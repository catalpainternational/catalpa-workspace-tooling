"""Backup credentials created on a quadlet env (``fcos/backup_credentials.py``), and an env's own
``spaces:`` in ``info.yaml``."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

import pytest

from catalpa_tooling import doctl_spaces_provision as dsp
from catalpa_tooling.config import load_project_config
from catalpa_tooling.fcos import backend
from catalpa_tooling.fcos import backup_credentials as bc

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"
INFO = {"docker_host": "ssh://deploy@staging.site.example/run/user/1001/podman/podman.sock"}

PGBR = {
    "pgbr_s3_write_bucket": "b",
    "pgbr_s3_write_region": "sgp1",
    "pgbr_s3_write_endpoint": "sgp1.digitaloceanspaces.com",
    "pgbr_s3_write_repo_path": "/p",
    "pgbr_s3_write_stanza": "main",
    "pgbr_s3_write_key": "AK",
    "pgbr_s3_write_secret": "SK",
}


@pytest.fixture
def swlt(tmp_path: Path):
    root = tmp_path / "swlt"
    shutil.copytree(FIXTURES / "swlt", root)
    shutil.copytree(root / "expected", root / "docker" / "envs" / "staging" / "generated" / "fcos")
    return load_project_config(root)


class Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes | None]] = []

    def __call__(self, argv, *, input=None, tty=False):
        self.calls.append((argv[-1], input))
        out = b""
        if "secret create" in argv[-1] and input:  # the push script confirms each secret
            out = b"".join(b"pushed " + line.split()[0] + b"\n" for line in input.splitlines())
        return subprocess.CompletedProcess(argv, 0, out, b"")


def test_which_commands_need_write_credentials() -> None:
    ns = argparse.Namespace(configure_mode="stanza-create")
    assert bc.needs_write("db", "configure", ns) == "pgbackrest"
    assert bc.needs_write("db", "configure", argparse.Namespace(configure_mode="verify")) is None
    assert bc.needs_write("db", "backup", ns) == "pgbackrest"
    assert bc.needs_write("db", "info", ns) is None
    assert bc.needs_write("db", "restore", ns) is None
    assert bc.needs_write("files", "init", ns) == "restic"
    assert bc.needs_write("files", "backup", ns) == "restic"
    assert bc.needs_write("files", "snapshots", ns) is None


def test_configured_credentials_change_nothing(swlt, monkeypatch) -> None:
    monkeypatch.setattr(dsp, "ensure_spaces_backup_credentials", lambda *a, **k: pytest.fail("provisioned"))
    r = Runner()
    b = backend.open_backend(swlt, "staging", INFO, runner=r, register_key=False)
    assert bc.ensure_backup_credentials(b, "pgbackrest", yes=True, dry_run=False, label="x",
                                        read_credentials=lambda: dict(PGBR)) == 0  # fmt: skip
    assert r.calls == []


def test_new_credentials_are_pushed_and_db_restarted(swlt, monkeypatch) -> None:
    store = {"django_secret_key": "d"}
    seen = {}

    def provision(config, env_name, env_add, creds_path, **kw):
        seen.update(env=env_name, target=kw["target"], yes=kw["yes"])
        store.update(PGBR)
        return 0

    monkeypatch.setattr(dsp, "ensure_spaces_backup_credentials", provision)
    r = Runner()
    b = backend.open_backend(swlt, "staging", INFO, runner=r, register_key=False)
    assert bc.ensure_backup_credentials(b, "pgbackrest", yes=True, dry_run=False, label="x",
                                        read_credentials=lambda: dict(store)) == 0  # fmt: skip
    assert seen == {"env": "staging", "target": "pgbackrest", "yes": True}
    import base64

    pushes = [inp.decode() for cmd, inp in r.calls if "secret create" in cmd]
    assert len(pushes) == 1  # one connection for them all
    lines = dict(line.split(" ", 1) for line in pushes[0].splitlines())
    assert sorted(lines) == sorted(PGBR)  # the changed ones only: not django_secret_key
    assert base64.b64decode(lines["pgbr_s3_write_key"]) == b"AK"
    assert not any("AK" in cmd or "SK" in cmd for cmd, _ in r.calls)  # values only on stdin
    assert r.calls[-1][0] == "systemctl --user restart db.service"


@pytest.mark.parametrize("rc,dry_run", [(1, False), (0, True)])
def test_declined_or_dry_run_pushes_nothing(swlt, monkeypatch, rc, dry_run) -> None:
    monkeypatch.setattr(dsp, "ensure_spaces_backup_credentials", lambda *a, **k: rc)
    r = Runner()
    b = backend.open_backend(swlt, "staging", INFO, runner=r, register_key=False)
    assert bc.ensure_backup_credentials(b, "restic", yes=False, dry_run=dry_run, label="x",
                                        read_credentials=dict) == rc  # fmt: skip
    assert r.calls == []


def test_an_env_can_have_its_own_bucket(swlt) -> None:
    info = swlt.deploy_envs_dir / "staging" / "info.yaml"
    d = dsp.spaces_backup_defaults(swlt, "staging")
    assert d.write_key_name == f"{d.bucket}-staging-write"
    info.write_text(info.read_text() + "spaces:\n  bucket: swlt-staging-backups\n")
    d = dsp.spaces_backup_defaults(swlt, "staging")
    assert d.bucket == "swlt-staging-backups"
    assert d.write_key_name == "swlt-staging-backups-staging-write"
    assert d.pgbackrest_repo_path == f"/{swlt.meta.name}/staging/pgbackrest"
    info.write_text(info.read_text() + "  nope: x\n")
    with pytest.raises(dsp.EnvSpacesError, match="unknown key"):
        dsp.spaces_backup_defaults(swlt, "staging")


def test_a_render_without_backups_says_to_render_first(swlt, monkeypatch, capsys) -> None:
    """New credentials the committed render doesn't use (it predates them): nothing is pushed,
    and dk says how to get the backup units onto the host."""
    store: dict = {}

    def provision(*a, **k):
        store.update(PGBR, pgbr_s3_write_uri_style="path")
        return 0

    monkeypatch.setattr(dsp, "ensure_spaces_backup_credentials", provision)
    r = Runner()
    b = backend.open_backend(swlt, "staging", INFO, runner=r, register_key=False)
    assert bc.ensure_backup_credentials(b, "pgbackrest", yes=True, dry_run=False, label="x",
                                        read_credentials=lambda: dict(store)) == 1  # fmt: skip
    assert r.calls == []
    assert "dk staging render" in capsys.readouterr().err
