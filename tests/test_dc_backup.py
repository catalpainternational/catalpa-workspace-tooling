"""Tests for DOCKER_ADD_HOST / DC_BACKUP_CA_FILE and dc-backup helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from catalpa_tooling.dc_backup.hosts import (
    DC_BACKUP_CA_CONTAINER_PATH,
    apply_inferred_dc_backup_ca_file,
    docker_add_host_args,
    docker_ca_env_flags_for_restic,
    docker_ca_volume_args,
    parse_docker_add_hosts,
    restic_cacert_argv,
    write_dc_backup_tls_override,
)
from catalpa_tooling.dc_backup.paths import APP_CA_FILENAME, DC_BACKUP_TLS_FILENAME
from catalpa_tooling.dc_backup.stack import render_garage_toml
from catalpa_tooling.dc_backup.tls import (
    KEY_CA_CRT,
    KEY_SERVER_CRT,
    KEY_SERVER_DNS,
    KEY_SERVER_IPS,
    KEY_SERVER_KEY,
    default_dc_backup_ca_file,
    generate_dc_backup_tls_material,
)
from catalpa_tooling.pgbackrest_volume_config import render_pgbackrest_ini


def test_parse_docker_add_hosts_comma_and_space() -> None:
    env = {"DOCKER_ADD_HOST": "s3.backup.internal:203.0.113.28, other:10.0.0.1"}
    assert parse_docker_add_hosts(env) == [
        ("s3.backup.internal", "203.0.113.28"),
        ("other", "10.0.0.1"),
    ]


def test_parse_docker_add_hosts_rejects_bad_entry() -> None:
    with pytest.raises(ValueError, match="hostname:IPv4"):
        parse_docker_add_hosts({"DOCKER_ADD_HOST": "not-an-ip"})


def test_docker_add_host_and_ca_args() -> None:
    env = {
        "DOCKER_ADD_HOST": "s3.backup.internal:203.0.113.28",
        "DC_BACKUP_CA_FILE": "/etc/indmo/tls/dc-backup-ca.crt",
    }
    assert docker_add_host_args(env) == [
        "--add-host",
        "s3.backup.internal:203.0.113.28",
    ]
    assert docker_ca_volume_args(env) == [
        "-v",
        f"/etc/indmo/tls/dc-backup-ca.crt:{DC_BACKUP_CA_CONTAINER_PATH}:ro",
    ]
    assert docker_ca_env_flags_for_restic(env) == [
        "-e",
        f"AWS_CA_BUNDLE={DC_BACKUP_CA_CONTAINER_PATH}",
    ]
    assert restic_cacert_argv(env) == ["--cacert", DC_BACKUP_CA_CONTAINER_PATH]
    assert restic_cacert_argv({}) == []


def test_dc_backup_ca_file_requires_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        docker_ca_volume_args({"DC_BACKUP_CA_FILE": "relative/ca.crt"})


def test_compose_override_extra_hosts_and_ca(minimal_project, tmp_path, monkeypatch) -> None:
    from catalpa_tooling.dc_backup import hosts as dht

    monkeypatch.setattr(dht, "local_proxy_data_dir", lambda: tmp_path)
    env = {
        "DOCKER_ADD_HOST": "s3.backup.internal:203.0.113.28",
        "DC_BACKUP_CA_FILE": "/etc/indmo/tls/dc-backup-ca.crt",
    }
    path = write_dc_backup_tls_override(minimal_project, "prod", env)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "extra_hosts:" in text
    assert '"s3.backup.internal:203.0.113.28"' in text
    assert f"{DC_BACKUP_CA_CONTAINER_PATH}:ro" in text
    assert "dc-backup-tls.yaml" in path.name


def test_pgbackrest_ini_emits_storage_ca_file() -> None:
    vars_map = {
        "BUCKET": "b",
        "ENDPOINT": "203.0.113.28",
        "REGION": "garage",
        "KEY": "k",
        "SECRET": "s",
        "REPO_PATH": "/r",
        "STANZA": "main",
    }
    env = {"DC_BACKUP_CA_FILE": "/etc/indmo/tls/dc-backup-ca.crt"}
    text = render_pgbackrest_ini("write", vars_map, env)
    assert f"repo1-storage-ca-file={DC_BACKUP_CA_CONTAINER_PATH}" in text


def test_pgbackrest_ini_omits_ca_when_unset() -> None:
    vars_map = {
        "BUCKET": "b",
        "ENDPOINT": "203.0.113.28",
        "REGION": "garage",
        "KEY": "k",
        "SECRET": "s",
        "REPO_PATH": "/r",
        "STANZA": "main",
    }
    text = render_pgbackrest_ini("write", vars_map, {})
    assert "repo1-storage-ca-file" not in text


def test_generate_dc_backup_tls_material_openssl() -> None:
    material = generate_dc_backup_tls_material(
        ips=["203.0.113.28"],
        dns_names=["s3.backup.internal"],
        days=30,
    )
    assert "BEGIN CERTIFICATE" in material[KEY_CA_CRT]
    assert "BEGIN CERTIFICATE" in material[KEY_SERVER_CRT]
    assert "PRIVATE KEY" in material[KEY_SERVER_KEY]
    assert material[KEY_SERVER_IPS] == ["203.0.113.28"]
    assert material[KEY_SERVER_DNS] == ["s3.backup.internal"]


def test_cmd_dc_backup_tls_issue_writes_via_sops(minimal_project) -> None:
    from catalpa_tooling.dc_backup.tls import cmd_dc_backup_tls_issue

    env_dir = minimal_project.deploy_envs_dir / "prod"
    env_dir.mkdir(parents=True, exist_ok=True)
    written: dict = {}

    def _fake_write(path: Path, data: dict) -> None:
        written["path"] = path
        written["data"] = data
        path.write_text("sops: encrypted\n", encoding="utf-8")

    with (
        patch("catalpa_tooling.dc_backup.tls.ensure_sops_available"),
        patch("catalpa_tooling.dc_backup.tls.write_encrypted_yaml", side_effect=_fake_write),
    ):
        rc = cmd_dc_backup_tls_issue(
            minimal_project,
            "prod",
            ips=["10.0.0.1"],
            dns_names=[],
            days=30,
            force=False,
            dry_run=False,
        )
    assert rc == 0
    assert written["path"].name == DC_BACKUP_TLS_FILENAME
    assert "BEGIN CERTIFICATE" in written["data"][KEY_CA_CRT]


def test_apply_inferred_dc_backup_ca_file_when_sops_exists(minimal_project) -> None:
    env_dir = minimal_project.deploy_envs_dir / "prod"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / DC_BACKUP_TLS_FILENAME).write_text("sops: stub\n", encoding="utf-8")
    env: dict[str, str] = {}
    apply_inferred_dc_backup_ca_file(env, minimal_project, "prod")
    assert env["DC_BACKUP_CA_FILE"] == default_dc_backup_ca_file(minimal_project)
    assert env["DC_BACKUP_CA_FILE"].endswith(APP_CA_FILENAME)


def test_apply_inferred_dc_backup_ca_file_skips_without_sops(minimal_project) -> None:
    env: dict[str, str] = {}
    apply_inferred_dc_backup_ca_file(env, minimal_project, "prod")
    assert "DC_BACKUP_CA_FILE" not in env


def test_apply_inferred_dc_backup_ca_file_explicit_wins(minimal_project) -> None:
    env_dir = minimal_project.deploy_envs_dir / "prod"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / DC_BACKUP_TLS_FILENAME).write_text("sops: stub\n", encoding="utf-8")
    env = {"DC_BACKUP_CA_FILE": "/custom/ca.crt"}
    apply_inferred_dc_backup_ca_file(env, minimal_project, "prod")
    assert env["DC_BACKUP_CA_FILE"] == "/custom/ca.crt"


def test_render_garage_toml_substitutes_secrets() -> None:
    text = render_garage_toml(
        rpc_secret="abc123",
        admin_token="tok",
        region="garage",
    )
    assert 'rpc_secret = "abc123"' in text
    assert 'admin_token = "tok"' in text
    assert 's3_region = "garage"' in text
    assert "@RPC_SECRET@" not in text


def test_restic_snapshots_userparameter_includes_add_host_and_ca() -> None:
    from catalpa_tooling.zabbix_systemd import _restic_snapshots_userparameter_command

    env = {
        "DOCKER_ADD_HOST": "s3.backup.internal:203.0.113.28",
        "DC_BACKUP_CA_FILE": "/etc/indmo/tls/dc-backup-ca.crt",
        "ZABBIX_RESTIC_DOCKER_ENV_FILE": "/etc/indmo/restic-files-backup.env",
    }
    cmd = _restic_snapshots_userparameter_command(env)
    assert "--add-host" in cmd
    assert "s3.backup.internal:203.0.113.28" in cmd
    assert DC_BACKUP_CA_CONTAINER_PATH in cmd
    assert "AWS_CA_BUNDLE=" in cmd
    assert "--cacert" in cmd


def test_cmd_dc_backup_bootstrap_writes_sops(minimal_project) -> None:
    from catalpa_tooling.dc_backup.stack import KEY_ADMIN, KEY_RPC, cmd_dc_backup_bootstrap

    env_dir = minimal_project.deploy_envs_dir / "prod"
    env_dir.mkdir(parents=True, exist_ok=True)
    written: dict = {}

    def _fake_write(path: Path, data: dict) -> None:
        written["path"] = path
        written["data"] = data
        path.write_text("sops: encrypted\n", encoding="utf-8")

    with (
        patch("catalpa_tooling.dc_backup.stack.ensure_sops_available"),
        patch("catalpa_tooling.dc_backup.stack.write_encrypted_yaml", side_effect=_fake_write),
    ):
        rc = cmd_dc_backup_bootstrap(minimal_project, "prod", force=False, dry_run=False)
    assert rc == 0
    assert written["path"].name == "dc-backup.yaml"
    assert len(written["data"][KEY_RPC]) == 64
    assert written["data"][KEY_ADMIN]
