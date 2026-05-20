"""Spaces backup auto-provisioning."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalpa_tooling.doctl_spaces_provision import (
    ensure_spaces_backup_credentials,
    needs_pgbr_write,
    needs_restic_write,
    pgbr_write_configured,
    restic_write_configured,
    spaces_backup_defaults,
)
from catalpa_tooling.config import load_project_config


@pytest.mark.parametrize(
    ("sub", "tail", "expected"),
    [
        ("backup", ["full"], True),
        ("configure", [], False),
        ("configure", ["verify"], False),
        ("configure", ["stanza-create"], True),
        ("pgdump", [], False),
        ("restore", [], False),
        ("install-systemd", [], True),
    ],
)
def test_needs_pgbr_write(sub: str, tail: list[str], expected: bool) -> None:
    assert needs_pgbr_write(sub, tail) is expected


@pytest.mark.parametrize(
    ("sub", "expected"),
    [
        ("backup", True),
        ("init", True),
        ("restore", False),
        ("snapshots", False),
        ("install-systemd", True),
    ],
)
def test_needs_restic_write(sub: str, expected: bool) -> None:
    assert needs_restic_write(sub) is expected


def test_pgbr_write_configured_complete() -> None:
    env = {
        "PGBR_S3_WRITE_BUCKET": "b",
        "PGBR_S3_WRITE_REGION": "sgp1",
        "PGBR_S3_WRITE_KEY": "k",
        "PGBR_S3_WRITE_SECRET": "s",
        "PGBR_S3_WRITE_REPO_PATH": "/p",
        "PGBR_S3_WRITE_STANZA": "main",
    }
    assert pgbr_write_configured(env)


def test_restic_write_configured_complete() -> None:
    env = {
        "RESTIC_WRITE_REPOSITORY": "s3:host/b/p",
        "RESTIC_WRITE_PASSWORD": "pw",
    }
    assert restic_write_configured(env)


def test_provision_pgbackrest_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    repo = _minimal_repo(tmp_path)
    monkeypatch.chdir(repo)
    cfg = load_project_config(repo)
    creds = repo / "docker/envs/prod/credentials.yaml"
    creds.parent.mkdir(parents=True)
    creds.write_text("sops: {}\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_spaces_provision.confirm_yes_default_no",
        lambda _p: True,
    )
    env: dict[str, str] = {}
    rc = ensure_spaces_backup_credentials(
        cfg,
        "prod",
        env,
        creds,
        target="pgbackrest",
        command_label="dk prod bkp_db backup",
        dry_run=True,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "dry-run" in err
    assert "pgbr_s3_write" in err


def test_provision_pgbackrest_calls_s3cmd_and_doctl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _minimal_repo(tmp_path)
    monkeypatch.chdir(repo)
    cfg = load_project_config(repo)
    creds = repo / "docker/envs/prod/credentials.yaml"
    creds.parent.mkdir(parents=True)
    creds.write_text("sops: {}\n", encoding="utf-8")
    defaults = spaces_backup_defaults(cfg, "prod")

    s3cmd_calls: list[list[str]] = []
    doctl_json_calls: list[list[str]] = []
    sops_sets: list[tuple[str, str]] = []

    def fake_s3cmd(args, **kwargs):
        from types import SimpleNamespace

        s3cmd_calls.append(list(args))
        cmd = args[0] if args else ""
        if cmd == "info":
            return SimpleNamespace(returncode=1, stdout="", stderr="404")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_doctl_json(args, *, context=None):
        doctl_json_calls.append(list(args))
        if "list" in args:
            return []
        return [{"name": defaults.write_key_name, "access_key": "AK123", "secret_key": "SEC456"}]

    def fake_apply(creds_path, values):
        sops_sets.extend(values.items())

    monkeypatch.setattr("catalpa_tooling.doctl_spaces_provision.ensure_doctl_available", lambda: Path("/doctl"))
    monkeypatch.setattr("catalpa_tooling.doctl_spaces_provision.ensure_s3cmd_available", lambda: Path("/s3cmd"))
    monkeypatch.setattr("catalpa_tooling.doctl_spaces_provision.run_s3cmd", fake_s3cmd)
    monkeypatch.setattr("catalpa_tooling.doctl_spaces_provision.run_doctl_json", fake_doctl_json)
    def fake_run_doctl(args, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("catalpa_tooling.doctl_spaces_provision.run_doctl", fake_run_doctl)
    monkeypatch.setattr("catalpa_tooling.doctl_spaces_provision.apply_credential_sets", fake_apply)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_spaces_provision.confirm_yes_default_no",
        lambda _p: True,
    )

    env: dict[str, str] = {}
    rc = ensure_spaces_backup_credentials(
        cfg,
        "prod",
        env,
        creds,
        target="pgbackrest",
        command_label="dk prod bkp_db backup",
        dry_run=False,
    )
    assert rc == 0
    assert any(c[0] == "mb" for c in s3cmd_calls)
    mb = next(c for c in s3cmd_calls if c[0] == "mb")
    assert f"s3://{defaults.bucket}" in mb
    assert any("--host=sgp1.digitaloceanspaces.com" in a for a in mb)
    assert any("keys" in c and "create" in c for c in doctl_json_calls)
    assert sops_sets
    keys = dict(sops_sets)
    assert keys["pgbr_s3_write_bucket"] == defaults.bucket
    assert keys["pgbr_s3_write_key"] == "AK123"


def test_provision_restic_reuses_pgbr_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _minimal_repo(tmp_path)
    monkeypatch.chdir(repo)
    cfg = load_project_config(repo)
    creds = repo / "docker/envs/prod/credentials.yaml"
    creds.parent.mkdir(parents=True)
    creds.write_text("sops: {}\n", encoding="utf-8")

    doctl_create_calls = 0
    sops_sets: dict[str, str] = {}

    def fake_doctl_json(args, *, context=None):
        nonlocal doctl_create_calls
        if "create" in args:
            doctl_create_calls += 1
        return []

    def fake_apply(_path, values):
        sops_sets.update(values)

    monkeypatch.setattr("catalpa_tooling.doctl_spaces_provision.ensure_doctl_available", lambda: Path("/doctl"))
    monkeypatch.setattr("catalpa_tooling.doctl_spaces_provision.run_doctl_json", fake_doctl_json)
    monkeypatch.setattr("catalpa_tooling.doctl_spaces_provision.apply_credential_sets", fake_apply)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_spaces_provision.confirm_yes_default_no",
        lambda _p: True,
    )

    env = {
        "PGBR_S3_WRITE_BUCKET": "bkt",
        "PGBR_S3_WRITE_REGION": "sgp1",
        "PGBR_S3_WRITE_ENDPOINT": "sgp1.digitaloceanspaces.com",
        "PGBR_S3_WRITE_KEY": "EXISTING",
        "PGBR_S3_WRITE_SECRET": "SECRET",
        "PGBR_S3_WRITE_REPO_PATH": "/p",
        "PGBR_S3_WRITE_STANZA": "main",
    }
    rc = ensure_spaces_backup_credentials(
        cfg,
        "prod",
        env,
        creds,
        target="restic",
        command_label="dk prod bkp_files backup",
        dry_run=False,
    )
    assert rc == 0
    assert doctl_create_calls == 0
    assert sops_sets["restic_write_s3_access_key_id"] == "EXISTING"
    assert sops_sets["restic_write_s3_secret_access_key"] == "SECRET"
    assert sops_sets["restic_write_repository"].startswith("s3:sgp1.digitaloceanspaces.com/bkt/")


def _minimal_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    (repo / "tooling.yaml").write_text(
        """
project:
  name: myapp
  root_marker: pyproject.toml
paths:
  backend: b
  frontend: f
  scripts: s
  env_local: .env
  email_backend_dir: e
  fetch_db_dump: d
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
stack:
  compose_project_default: app
  services: {web: w, proxy: p, db: db}
  images:
    registry_key: reg
    components: {web: w, proxy: p, db: db}
  healthcheck: {service: w, url: http://localhost/}
ops:
  install_prefix: /opt/a
  config_dir: /etc/a
  systemd_unit_prefix: a-
  transfer_workdir: .t
  default_db_container: db1
  pgbackrest:
    postgres_conf: a.conf
    pgbackrest_conf: b.conf
    default_registry: ghcr.io/x
    restore_temp_prefix: p_
  zabbix:
    unit_name: z.service
    userparams_file: z.conf
  systemd_units:
    pgbackrest:
      - a-pgbackrest-backup-full.service
      - a-pgbackrest-backup-incr.service
      - a-pgbackrest-backup-diff.service
      - a-pgbackrest-backup-full.timer
    restic:
      - a-restic-files-backup.service
      - a-restic-files-backup.timer
""",
        encoding="utf-8",
    )
    return repo
