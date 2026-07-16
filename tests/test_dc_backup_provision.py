"""Garage dc-backup provision unit tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.dc_backup.provision import (
    GarageAccessKey,
    GarageProvisionError,
    build_credential_values,
    cmd_dc_backup_provision,
    ensure_garage_key,
    find_garage_keys_by_name,
    garage_backup_defaults,
    looks_like_spaces_endpoint,
    parse_garage_key_info,
    parse_garage_key_list,
    parse_garage_node_id,
    parse_restic_s3_repository,
    write_credentials_look_like_spaces,
)
from catalpa_tooling.dc_backup.tls import KEY_SERVER_IPS
from tests.helpers import install_in_memory_sops_mocks, write_minimal_tooling_tree

_KEY_INFO = """\
Key name: minimal-prod-backup
Key ID: GK3515373e4c851ebaad366558
Secret key: 7d37d093435a41f2aab8f13c19ba067d9776c90215f56614adad6ece597dbb34
Authorized buckets:
"""

_STATUS_NO_ROLE = """\
==== HEALTHY NODES ====
ID                Hostname  Address         Tags  Zone  Capacity          DataAvail  Version
563e1ac825ee3323  linuxbox  127.0.0.1:3901              NO ROLE ASSIGNED             v2.3.0
"""

_STATUS_OK = """\
==== HEALTHY NODES ====
ID                Hostname  Address         Tags       Zone  Capacity  DataAvail         Version
563e1ac825ee3323  linuxbox  127.0.0.1:3901  [default]  dc    300 GiB   290 GiB (97.0%)  v2.3.0
"""


def _seed_env(tmp_path: Path) -> tuple[object, Path, Path, Path]:
    write_minimal_tooling_tree(tmp_path)
    cfg = load_project_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "info.yaml").write_text(
        "docker_host: ssh://root@203.0.113.27\n"
        "dc_backup_docker_host: ssh://root@203.0.113.28\n",
        encoding="utf-8",
    )
    creds = env_dir / "credentials.yaml"
    creds.write_text("sops: {}\n", encoding="utf-8")
    tls = env_dir / "dc-backup-tls.yaml"
    tls.write_text("sops: {}\n", encoding="utf-8")
    stack = env_dir / "dc-backup.yaml"
    stack.write_text("sops: {}\n", encoding="utf-8")
    return cfg, creds, tls, stack


def _stub_s3_cli_install(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Avoid SSH during provision tests; record install kwargs."""
    calls: list[dict] = []

    def fake(*_a, **kwargs):
        calls.append(dict(kwargs))

    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.provision._try_install_garage_s3_cli",
        fake,
    )
    return calls


def test_parse_garage_key_info() -> None:
    key = parse_garage_key_info(_KEY_INFO)
    assert key.access_key_id.startswith("GK")
    assert len(key.secret_access_key) == 64


def test_parse_garage_key_list_and_find() -> None:
    text = (
        "ID                          Name\n"
        "GK111111111111111111111111  other-key\n"
        "GK3515373e4c851ebaad366558  minimal-prod-backup\n"
        "GK222222222222222222222222  minimal-prod-backup\n"
    )
    rows = parse_garage_key_list(text)
    assert ("GK3515373e4c851ebaad366558", "minimal-prod-backup") in rows
    matches = find_garage_keys_by_name(text, "minimal-prod-backup")
    assert len(matches) == 2


def test_ensure_garage_key_reuses_unique(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_capture(ssh: str, cmd: str, *, dry_run: bool = False):
        if "key list" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "ID\tName\n"
                    "GK3515373e4c851ebaad366558\tminimal-prod-backup\n"
                ),
                stderr="",
            )
        if "key info" in cmd and "GK3515373e4c851ebaad366558" in cmd:
            return SimpleNamespace(returncode=0, stdout=_KEY_INFO, stderr="")
        if "key create" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="should not create")
        return SimpleNamespace(returncode=1, stdout="", stderr=cmd)

    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.provision.remote_run_capture",
        fake_capture,
    )
    access = ensure_garage_key("root@host", "minimal-prod-backup", dry_run=False)
    assert access.access_key_id == "GK3515373e4c851ebaad366558"
    assert "already exists" in capsys.readouterr().out


def test_ensure_garage_key_rejects_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_capture(ssh: str, cmd: str, *, dry_run: bool = False):
        if "key list" in cmd:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "ID\tName\n"
                    "GK111111111111111111111111\tindmo-prod-backup\n"
                    "GK222222222222222222222222\tindmo-prod-backup\n"
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=1, stdout="", stderr=cmd)

    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.provision.remote_run_capture",
        fake_capture,
    )
    with pytest.raises(GarageProvisionError, match="2 keys named"):
        ensure_garage_key("root@host", "indmo-prod-backup", dry_run=False)


def test_parse_garage_key_info_missing_secret() -> None:
    with pytest.raises(GarageProvisionError, match="Could not parse"):
        parse_garage_key_info("Key name: x\nKey ID: GKabc\n")


def test_parse_garage_node_id() -> None:
    assert parse_garage_node_id(_STATUS_NO_ROLE) == "563e1ac825ee3323"
    assert parse_garage_node_id(_STATUS_OK) == "563e1ac825ee3323"


def test_parse_restic_s3_repository() -> None:
    assert parse_restic_s3_repository("s3:203.0.113.28/indmo-backups/indmo-prod-media") == (
        "203.0.113.28",
        "indmo-backups",
        "indmo-prod-media",
    )


def test_looks_like_spaces_endpoint() -> None:
    assert looks_like_spaces_endpoint("sgp1.digitaloceanspaces.com")
    assert not looks_like_spaces_endpoint("203.0.113.28")
    assert write_credentials_look_like_spaces(
        {"PGBR_S3_WRITE_ENDPOINT": "nyc3.digitaloceanspaces.com"}
    )


def test_garage_backup_defaults(minimal_project) -> None:
    d = garage_backup_defaults(
        minimal_project,
        "prod",
        info={"dc_backup_docker_host": "ssh://root@203.0.113.28"},
        tls_data={KEY_SERVER_IPS: ["203.0.113.28"]},
    )
    assert d.bucket == "minimal-backups"
    assert d.key_name == "minimal-prod-backup"
    assert d.endpoint == "203.0.113.28"
    assert d.pgbackrest_repo_path == "/minimal/prod/pgbackrest"
    assert d.restic_path == "minimal-prod-media"


def test_build_credential_values_shape() -> None:
    from catalpa_tooling.dc_backup.provision import GarageBackupDefaults

    defaults = GarageBackupDefaults(
        endpoint="203.0.113.28",
        region="garage",
        bucket="minimal-backups",
        key_name="minimal-prod-backup",
        pgbackrest_repo_path="/minimal/prod/pgbackrest",
        restic_path="minimal-prod-media",
        stanza="main",
        capacity="300G",
    )
    values = build_credential_values(
        defaults,
        GarageAccessKey("GKabc", "secrethex"),
        restic_password="keep-me",
    )
    assert values["pgbr_s3_write_uri_style"] == "path"
    assert values["pgbr_s3_write_verify_tls"] == "y"
    assert values["restic_write_password"] == "keep-me"
    assert values["restic_write_repository"] == (
        "s3:203.0.113.28/minimal-backups/minimal-prod-media"
    )


def test_provision_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg, _creds, tls, stack = _seed_env(tmp_path)
    store = install_in_memory_sops_mocks(monkeypatch)
    store[str(tls)] = {KEY_SERVER_IPS: ["203.0.113.28"]}
    store[str(stack)] = {"garage_admin_token": "tok", "garage_s3_region": "garage"}

    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("dry-run must not call Garage")

    monkeypatch.setattr("catalpa_tooling.dc_backup.provision.remote_run_capture", boom)

    rc = cmd_dc_backup_provision(cfg, "prod", dry_run=True, yes=True)
    assert rc == 0
    assert called["n"] == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "minimal-backups" in out
    assert "would sops set" in out
    assert "garage-s3" in out


def test_provision_cli_accepts_trailing_dry_run() -> None:
    """``dk <env> dc-backup provision --dry-run`` must parse (not only global --dry-run)."""
    import argparse
    import tempfile
    from pathlib import Path

    from catalpa_tooling.config import load_project_config
    from catalpa_tooling.env_parser import attach_env_subparsers
    from tests.helpers import write_minimal_tooling_tree

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        write_minimal_tooling_tree(root)
        (root / "docker" / "envs" / "prod").mkdir(parents=True, exist_ok=True)
        (root / "docker" / "envs" / "prod" / "info.yaml").write_text(
            "dc_backup_docker_host: ssh://root@203.0.113.28\n",
            encoding="utf-8",
        )
        cfg = load_project_config(root)
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="dk_command")
        attach_env_subparsers(sub, cfg)

        ns_trailing = p.parse_args(["prod", "dc-backup", "provision", "--dry-run"])
        assert ns_trailing.dc_backup_provision_dry_run is True
        # Must not clobber parent dest when only trailing flag is used
        assert ns_trailing.dry_run is False

        ns_global = p.parse_args(["prod", "--dry-run", "dc-backup", "provision"])
        assert ns_global.dry_run is True
        assert not ns_global.dc_backup_provision_dry_run


def test_provision_print_only_skips_sops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg, creds, tls, stack = _seed_env(tmp_path)
    store = install_in_memory_sops_mocks(monkeypatch)
    store[str(tls)] = {KEY_SERVER_IPS: ["203.0.113.28"]}
    store[str(stack)] = {"garage_admin_token": "tok", "garage_s3_region": "garage"}
    applied: list[dict[str, str]] = []

    def capture_apply(path: Path, values: dict[str, str]) -> None:
        applied.append(dict(values))

    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.provision.apply_credential_sets",
        capture_apply,
    )
    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.provision.ensure_ssh_known_host_for_docker_host",
        lambda *_a, **_k: 0,
    )
    s3_calls = _stub_s3_cli_install(monkeypatch)

    def fake_capture(ssh: str, cmd: str, *, dry_run: bool = False):
        if "status" in cmd:
            return SimpleNamespace(returncode=0, stdout=_STATUS_OK, stderr="")
        if "bucket create" in cmd:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        if "key list" in cmd:
            return SimpleNamespace(returncode=0, stdout="ID\tName\n", stderr="")
        if "key create" in cmd:
            return SimpleNamespace(returncode=0, stdout=_KEY_INFO, stderr="")
        if "bucket allow" in cmd:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr=f"unexpected: {cmd}")

    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.provision.remote_run_capture",
        fake_capture,
    )

    rc = cmd_dc_backup_provision(cfg, "prod", print_only=True, yes=True)
    assert rc == 0
    assert applied == []
    assert len(s3_calls) == 1
    assert s3_calls[0]["access"].access_key_id == "GK3515373e4c851ebaad366558"
    out = capsys.readouterr().out
    assert "pgbr_s3_write_key" in out
    assert "GK3515373e4c851ebaad366558" in out
    assert "Printed only" in out
    assert str(creds) in out


def test_provision_writes_sops_with_yes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, creds, tls, stack = _seed_env(tmp_path)
    store = install_in_memory_sops_mocks(monkeypatch)
    store[str(tls)] = {KEY_SERVER_IPS: ["203.0.113.28"]}
    store[str(stack)] = {"garage_admin_token": "tok", "garage_s3_region": "garage"}

    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.provision.ensure_ssh_known_host_for_docker_host",
        lambda *_a, **_k: 0,
    )
    _stub_s3_cli_install(monkeypatch)

    def fake_capture(ssh: str, cmd: str, *, dry_run: bool = False):
        if "status" in cmd:
            return SimpleNamespace(returncode=0, stdout=_STATUS_OK, stderr="")
        if "bucket create" in cmd:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        if "key list" in cmd:
            return SimpleNamespace(returncode=0, stdout="ID\tName\n", stderr="")
        if "key create" in cmd:
            return SimpleNamespace(returncode=0, stdout=_KEY_INFO, stderr="")
        if "bucket allow" in cmd:
            assert "GK3515373e4c851ebaad366558" in cmd
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr=cmd)

    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.provision.remote_run_capture",
        fake_capture,
    )

    rc = cmd_dc_backup_provision(cfg, "prod", yes=True)
    assert rc == 0
    written = store[str(creds)]
    assert written["pgbr_s3_write_bucket"] == "minimal-backups"
    assert written["pgbr_s3_write_uri_style"] == "path"
    assert written["pgbr_s3_write_key"] == "GK3515373e4c851ebaad366558"
    assert written["restic_write_s3_access_key_id"] == "GK3515373e4c851ebaad366558"


def test_provision_noop_when_already_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg, creds, tls, stack = _seed_env(tmp_path)
    store = install_in_memory_sops_mocks(monkeypatch)
    store[str(tls)] = {KEY_SERVER_IPS: ["203.0.113.28"]}
    store[str(stack)] = {"garage_admin_token": "tok"}
    store[str(creds)] = {
        "pgbr_s3_write_bucket": "minimal-backups",
        "pgbr_s3_write_region": "garage",
        "pgbr_s3_write_endpoint": "203.0.113.28",
        "pgbr_s3_write_key": "GKold",
        "pgbr_s3_write_secret": "oldsecret",
        "pgbr_s3_write_repo_path": "/minimal/prod/pgbackrest",
        "pgbr_s3_write_stanza": "main",
        "restic_write_repository": "s3:203.0.113.28/minimal-backups/minimal-prod-media",
        "restic_write_password": "pw",
        "restic_write_s3_access_key_id": "GKold",
        "restic_write_s3_secret_access_key": "oldsecret",
    }
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("should not call Garage")

    monkeypatch.setattr("catalpa_tooling.dc_backup.provision.remote_run_capture", boom)
    s3_calls = _stub_s3_cli_install(monkeypatch)

    rc = cmd_dc_backup_provision(cfg, "prod", yes=True)
    assert rc == 0
    assert called["n"] == 0
    assert len(s3_calls) == 1
    assert s3_calls[0]["access"].access_key_id == "GKold"
    assert s3_calls[0]["admin_token"] == "tok"
    assert "already configured" in capsys.readouterr().out


def test_provision_refuses_spaces_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg, creds, tls, stack = _seed_env(tmp_path)
    store = install_in_memory_sops_mocks(monkeypatch)
    store[str(tls)] = {KEY_SERVER_IPS: ["203.0.113.28"]}
    store[str(stack)] = {"garage_admin_token": "tok"}
    store[str(creds)] = {
        "pgbr_s3_write_bucket": "spaces-b",
        "pgbr_s3_write_region": "sgp1",
        "pgbr_s3_write_endpoint": "sgp1.digitaloceanspaces.com",
        "pgbr_s3_write_key": "AK",
        "pgbr_s3_write_secret": "SEC",
        "pgbr_s3_write_repo_path": "/p",
        "pgbr_s3_write_stanza": "main",
    }

    rc = cmd_dc_backup_provision(cfg, "prod", yes=True)
    assert rc == 1
    assert "DigitalOcean Spaces" in capsys.readouterr().err


def test_provision_partial_fill_restic_from_pgbr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, creds, tls, stack = _seed_env(tmp_path)
    store = install_in_memory_sops_mocks(monkeypatch)
    store[str(tls)] = {KEY_SERVER_IPS: ["203.0.113.28"]}
    store[str(stack)] = {"garage_admin_token": "tok"}
    store[str(creds)] = {
        "pgbr_s3_write_bucket": "minimal-backups",
        "pgbr_s3_write_region": "garage",
        "pgbr_s3_write_endpoint": "203.0.113.28",
        "pgbr_s3_write_key": "GKreuse",
        "pgbr_s3_write_secret": "secretreuse",
        "pgbr_s3_write_repo_path": "/minimal/prod/pgbackrest",
        "pgbr_s3_write_stanza": "main",
    }

    def boom(*_a, **_k):
        raise AssertionError("should not call Garage")

    def boom_ssh(*_a, **_k):
        raise AssertionError("should not ssh-keyscan when reusing keys")

    monkeypatch.setattr("catalpa_tooling.dc_backup.provision.remote_run_capture", boom)
    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.provision.ensure_ssh_known_host_for_docker_host",
        boom_ssh,
    )
    _stub_s3_cli_install(monkeypatch)

    rc = cmd_dc_backup_provision(cfg, "prod", yes=True)
    assert rc == 0
    written = store[str(creds)]
    assert written["restic_write_s3_access_key_id"] == "GKreuse"
    assert written["restic_write_s3_secret_access_key"] == "secretreuse"
    assert written["pgbr_s3_write_key"] == "GKreuse"
    assert written["restic_write_repository"].startswith("s3:203.0.113.28/")
