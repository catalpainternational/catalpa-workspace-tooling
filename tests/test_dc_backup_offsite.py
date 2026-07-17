"""dc-backup offsite (rclone Garage → external S3) tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.dc_backup.offsite import (
    GARAGE_LOOPBACK_ENDPOINT,
    UNIT_SUFFIX_SERVICE,
    UNIT_SUFFIX_TIMER,
    cmd_dc_backup_offsite_install,
    offsite_unit_names,
    render_rclone_offsite_env,
    validate_offsite_env,
)
from catalpa_tooling.systemd_render import render_systemd_unit, template_suffix_for_unit
from tests.helpers import install_in_memory_sops_mocks, write_minimal_tooling_tree


def _complete_env() -> dict[str, str]:
    return {
        "PGBR_S3_WRITE_BUCKET": "minimal-backups",
        "PGBR_S3_WRITE_KEY": "GKabc",
        "PGBR_S3_WRITE_SECRET": "secret",
        "PGBR_S3_WRITE_REGION": "garage",
        "OFFSITE_S3_BUCKET": "offsite-backups",
        "OFFSITE_S3_ACCESS_KEY_ID": "AKIA",
        "OFFSITE_S3_SECRET_ACCESS_KEY": "secret2",
        "OFFSITE_S3_REGION": "sgp1",
        "OFFSITE_S3_ENDPOINT": "sgp1.digitaloceanspaces.com",
    }


def test_validate_offsite_env_ok() -> None:
    assert validate_offsite_env(_complete_env()) is None


def test_validate_offsite_env_missing_garage() -> None:
    env = _complete_env()
    del env["PGBR_S3_WRITE_KEY"]
    err = validate_offsite_env(env)
    assert err is not None
    assert "PGBR_S3_WRITE_KEY" in err


def test_validate_offsite_env_missing_offsite() -> None:
    env = _complete_env()
    del env["OFFSITE_S3_BUCKET"]
    err = validate_offsite_env(env)
    assert err is not None
    assert "OFFSITE_S3_BUCKET" in err


def test_render_rclone_offsite_env_shape() -> None:
    body = render_rclone_offsite_env(_complete_env(), project_name="minimal")
    assert "GARAGE_ENDPOINT=http://127.0.0.1:3900" in body
    assert body.count(GARAGE_LOOPBACK_ENDPOINT) == 1
    assert "GARAGE_BUCKET=minimal-backups" in body
    assert "OFFSITE_S3_BUCKET=offsite-backups" in body
    assert "OFFSITE_S3_PROVIDER=Other" in body
    assert "OFFSITE_S3_PREFIX=" in body


def test_offsite_unit_names(minimal_project) -> None:
    service, timer = offsite_unit_names(minimal_project)
    assert service.endswith(UNIT_SUFFIX_SERVICE)
    assert timer.endswith(UNIT_SUFFIX_TIMER)
    assert template_suffix_for_unit(service) == UNIT_SUFFIX_SERVICE
    assert template_suffix_for_unit(timer) == UNIT_SUFFIX_TIMER


def test_render_offsite_timer_calendar() -> None:
    body = render_systemd_unit(
        "app-rclone-garage-offsite.timer",
        install_prefix="/opt/x",
        config_dir="/etc/x",
    )
    assert "OnCalendar=*-*-* 05:00:00" in body
    assert "Persistent=true" in body
    # Host local time: OnCalendar must not append UTC.
    for line in body.splitlines():
        if line.startswith("OnCalendar="):
            assert not line.strip().endswith("UTC")
            break
    else:
        raise AssertionError("missing OnCalendar=")
    assert "system timezone" in body


def test_render_offsite_service_paths() -> None:
    body = render_systemd_unit(
        "app-rclone-garage-offsite.service",
        install_prefix="/opt/indmo",
        config_dir="/etc/indmo",
    )
    assert "EnvironmentFile=-/etc/indmo/rclone-garage-offsite.env" in body
    assert "ExecStart=/opt/indmo/rclone-garage-offsite.sh" in body


def _seed_offsite_env(tmp_path: Path) -> tuple[object, Path]:
    write_minimal_tooling_tree(tmp_path)
    cfg = load_project_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "info.yaml").write_text(
        "docker_host: ssh://root@172.16.92.27\n"
        "dc_backup_docker_host: ssh://root@172.16.92.28\n",
        encoding="utf-8",
    )
    creds = env_dir / "credentials.yaml"
    creds.write_text("sops: {}\n", encoding="utf-8")
    return cfg, creds


def test_offsite_install_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg, creds = _seed_offsite_env(tmp_path)
    store = install_in_memory_sops_mocks(monkeypatch)
    store[str(creds)] = {
        "pgbr_s3_write_bucket": "minimal-backups",
        "pgbr_s3_write_key": "GKabc",
        "pgbr_s3_write_secret": "secret",
        "pgbr_s3_write_region": "garage",
        "offsite_s3_bucket": "offsite-backups",
        "offsite_s3_access_key_id": "AKIA",
        "offsite_s3_secret_access_key": "secret2",
        "offsite_s3_region": "sgp1",
        "offsite_s3_endpoint": "sgp1.digitaloceanspaces.com",
    }

    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("dry-run must not mutate remote")

    monkeypatch.setattr("catalpa_tooling.dc_backup.offsite.install_files_via_ssh", boom)
    monkeypatch.setattr("catalpa_tooling.dc_backup.offsite.remote_run", boom)

    rc = cmd_dc_backup_offsite_install(cfg, "prod", enable=False, yes=True, dry_run=True)
    assert rc == 0
    assert called["n"] == 0
    out = capsys.readouterr().out
    assert "dry_run=True" in out or "dry-run" in out
    assert "rclone-garage-offsite.env" in out
    assert "<redacted>" in out


def test_offsite_install_refuses_missing_offsite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg, creds = _seed_offsite_env(tmp_path)
    store = install_in_memory_sops_mocks(monkeypatch)
    store[str(creds)] = {
        "pgbr_s3_write_bucket": "minimal-backups",
        "pgbr_s3_write_key": "GKabc",
        "pgbr_s3_write_secret": "secret",
    }
    rc = cmd_dc_backup_offsite_install(cfg, "prod", enable=False, yes=True, dry_run=True)
    assert rc == 1
    assert "offsite_s3" in capsys.readouterr().err
