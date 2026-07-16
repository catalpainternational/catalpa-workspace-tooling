"""Tests for DOCKER_ADD_HOST / BACKUP_CA_FILE and backup-tls helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from catalpa_tooling.backup_tls import (
    KEY_CA_CRT,
    KEY_SERVER_CRT,
    KEY_SERVER_DNS,
    KEY_SERVER_IPS,
    KEY_SERVER_KEY,
    generate_backup_tls_material,
)
from catalpa_tooling.docker_host_tls import (
    BACKUP_CA_CONTAINER_PATH,
    docker_add_host_args,
    docker_ca_env_flags_for_restic,
    docker_ca_volume_args,
    parse_docker_add_hosts,
    write_docker_host_tls_override,
)
from catalpa_tooling.pgbackrest_volume_config import render_pgbackrest_ini


def test_parse_docker_add_hosts_comma_and_space() -> None:
    env = {"DOCKER_ADD_HOST": "s3.backup.internal:172.16.92.28, other:10.0.0.1"}
    assert parse_docker_add_hosts(env) == [
        ("s3.backup.internal", "172.16.92.28"),
        ("other", "10.0.0.1"),
    ]


def test_parse_docker_add_hosts_rejects_bad_entry() -> None:
    with pytest.raises(ValueError, match="hostname:IPv4"):
        parse_docker_add_hosts({"DOCKER_ADD_HOST": "not-an-ip"})


def test_docker_add_host_and_ca_args() -> None:
    env = {
        "DOCKER_ADD_HOST": "s3.backup.internal:172.16.92.28",
        "BACKUP_CA_FILE": "/etc/indmo/tls/backup-ca.crt",
    }
    assert docker_add_host_args(env) == [
        "--add-host",
        "s3.backup.internal:172.16.92.28",
    ]
    assert docker_ca_volume_args(env) == [
        "-v",
        f"/etc/indmo/tls/backup-ca.crt:{BACKUP_CA_CONTAINER_PATH}:ro",
    ]
    assert docker_ca_env_flags_for_restic(env) == [
        "-e",
        f"AWS_CA_BUNDLE={BACKUP_CA_CONTAINER_PATH}",
    ]


def test_backup_ca_file_requires_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        docker_ca_volume_args({"BACKUP_CA_FILE": "relative/ca.crt"})


def test_compose_override_extra_hosts_and_ca(minimal_project, tmp_path, monkeypatch) -> None:
    from catalpa_tooling import docker_host_tls as dht

    monkeypatch.setattr(dht, "local_proxy_data_dir", lambda: tmp_path)
    env = {
        "DOCKER_ADD_HOST": "s3.backup.internal:172.16.92.28",
        "BACKUP_CA_FILE": "/etc/indmo/tls/backup-ca.crt",
    }
    path = write_docker_host_tls_override(minimal_project, "prod", env)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "extra_hosts:" in text
    assert '"s3.backup.internal:172.16.92.28"' in text
    assert f"{BACKUP_CA_CONTAINER_PATH}:ro" in text


def test_pgbackrest_ini_emits_storage_ca_file() -> None:
    vars_map = {
        "BUCKET": "b",
        "ENDPOINT": "172.16.92.28",
        "REGION": "garage",
        "KEY": "k",
        "SECRET": "s",
        "REPO_PATH": "/r",
        "STANZA": "main",
    }
    env = {"BACKUP_CA_FILE": "/etc/indmo/tls/backup-ca.crt"}
    text = render_pgbackrest_ini("write", vars_map, env)
    assert f"repo1-storage-ca-file={BACKUP_CA_CONTAINER_PATH}" in text


def test_pgbackrest_ini_omits_ca_when_unset() -> None:
    vars_map = {
        "BUCKET": "b",
        "ENDPOINT": "172.16.92.28",
        "REGION": "garage",
        "KEY": "k",
        "SECRET": "s",
        "REPO_PATH": "/r",
        "STANZA": "main",
    }
    text = render_pgbackrest_ini("write", vars_map, {})
    assert "repo1-storage-ca-file" not in text


def test_generate_backup_tls_material_openssl() -> None:
    material = generate_backup_tls_material(
        ips=["172.16.92.28"],
        dns_names=["s3.backup.internal"],
        days=30,
    )
    assert "BEGIN CERTIFICATE" in material[KEY_CA_CRT]
    assert "BEGIN CERTIFICATE" in material[KEY_SERVER_CRT]
    assert "PRIVATE KEY" in material[KEY_SERVER_KEY]
    assert material[KEY_SERVER_IPS] == ["172.16.92.28"]
    assert material[KEY_SERVER_DNS] == ["s3.backup.internal"]


def test_cmd_backup_tls_issue_writes_via_sops(minimal_project, tmp_path) -> None:
    from catalpa_tooling.backup_tls import cmd_backup_tls_issue

    env_dir = minimal_project.deploy_envs_dir / "prod"
    env_dir.mkdir(parents=True, exist_ok=True)
    written: dict = {}

    def _fake_write(path: Path, data: dict) -> None:
        written["path"] = path
        written["data"] = data
        path.write_text("sops: encrypted\n", encoding="utf-8")

    with (
        patch("catalpa_tooling.backup_tls.ensure_sops_available"),
        patch("catalpa_tooling.backup_tls.write_encrypted_yaml", side_effect=_fake_write),
    ):
        rc = cmd_backup_tls_issue(
            minimal_project,
            "prod",
            ips=["10.0.0.1"],
            dns_names=[],
            days=30,
            force=False,
            dry_run=False,
        )
    assert rc == 0
    assert written["path"].name == "backup-tls.yaml"
    assert "BEGIN CERTIFICATE" in written["data"][KEY_CA_CRT]


def test_apply_inferred_backup_ca_file_when_sops_exists(minimal_project) -> None:
    from catalpa_tooling.backup_tls import default_backup_ca_file
    from catalpa_tooling.docker_host_tls import apply_inferred_backup_ca_file

    env_dir = minimal_project.deploy_envs_dir / "prod"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "backup-tls.yaml").write_text("sops: stub\n", encoding="utf-8")
    env: dict[str, str] = {}
    apply_inferred_backup_ca_file(env, minimal_project, "prod")
    assert env["BACKUP_CA_FILE"] == default_backup_ca_file(minimal_project)


def test_apply_inferred_backup_ca_file_skips_without_sops(minimal_project) -> None:
    from catalpa_tooling.docker_host_tls import apply_inferred_backup_ca_file

    env: dict[str, str] = {}
    apply_inferred_backup_ca_file(env, minimal_project, "prod")
    assert "BACKUP_CA_FILE" not in env


def test_apply_inferred_backup_ca_file_explicit_wins(minimal_project) -> None:
    from catalpa_tooling.docker_host_tls import apply_inferred_backup_ca_file

    env_dir = minimal_project.deploy_envs_dir / "prod"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "backup-tls.yaml").write_text("sops: stub\n", encoding="utf-8")
    env = {"BACKUP_CA_FILE": "/custom/ca.crt"}
    apply_inferred_backup_ca_file(env, minimal_project, "prod")
    assert env["BACKUP_CA_FILE"] == "/custom/ca.crt"


def test_restic_snapshots_userparameter_includes_add_host_and_ca() -> None:
    from catalpa_tooling.zabbix_systemd import _restic_snapshots_userparameter_command

    env = {
        "DOCKER_ADD_HOST": "s3.backup.internal:172.16.92.28",
        "BACKUP_CA_FILE": "/etc/indmo/tls/backup-ca.crt",
        "ZABBIX_RESTIC_DOCKER_ENV_FILE": "/etc/indmo/restic-files-backup.env",
    }
    cmd = _restic_snapshots_userparameter_command(env)
    assert "--add-host" in cmd
    assert "s3.backup.internal:172.16.92.28" in cmd
    assert BACKUP_CA_CONTAINER_PATH in cmd
    assert "AWS_CA_BUNDLE=" in cmd
