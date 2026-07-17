"""Tests for garage-s3 host CLI helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.dc_backup.s3_cli import (
    ADMIN_SCRIPT_NAME,
    ENV_FILENAME,
    S3_SCRIPT_NAME,
    _render_s3_script,
    install_garage_s3_cli,
    render_garage_s3_env,
)
from tests.helpers import write_minimal_tooling_tree


def test_render_garage_s3_env_with_admin() -> None:
    body = render_garage_s3_env(
        project_name="minimal",
        access_key_id="GKabc",
        secret_access_key="secrethex",
        bucket="minimal-backups",
        region="garage",
        admin_token="admin-tok",
    )
    assert "AWS_ACCESS_KEY_ID=GKabc" in body
    assert "AWS_SECRET_ACCESS_KEY=secrethex" in body
    assert "AWS_ENDPOINT_URL=http://127.0.0.1:3900" in body
    assert "GARAGE_BUCKET=minimal-backups" in body
    assert "GARAGE_ADMIN_TOKEN=admin-tok" in body


def test_render_garage_s3_env_omits_empty_admin() -> None:
    body = render_garage_s3_env(
        project_name="minimal",
        access_key_id="GKabc",
        secret_access_key="secrethex",
        bucket="b",
        region="garage",
        admin_token="",
    )
    assert "GARAGE_ADMIN_TOKEN" not in body


def test_render_s3_script_substitutes_config_dir() -> None:
    text = _render_s3_script(config_dir="/etc/app")
    assert "/etc/app/garage-s3.env" in text
    assert "@CONFIG_DIR@" not in text
    assert "command -v aws" in text
    assert "docker run" in text
    assert "amazon/aws-cli" in text


def test_install_garage_s3_cli_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_minimal_tooling_tree(tmp_path)
    cfg = load_project_config(tmp_path)
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("dry-run must not scp")

    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.s3_cli.install_files_via_ssh",
        boom,
    )
    install_garage_s3_cli(
        "root@203.0.113.28",
        cfg,
        access_key_id="GKabc",
        secret_access_key="secret",
        bucket="minimal-backups",
        region="garage",
        admin_token="tok",
        dry_run=True,
    )
    assert called["n"] == 0
    out = capsys.readouterr().out
    assert ENV_FILENAME in out
    assert "AWS_SECRET_ACCESS_KEY=<redacted>" in out or "<redacted>" in out
    assert S3_SCRIPT_NAME in out


def test_install_garage_s3_cli_copies_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_tooling_tree(tmp_path)
    cfg = load_project_config(tmp_path)
    installs: list[tuple[str, list[str]]] = []
    remote_cmds: list[str] = []

    def fake_install(ssh: str, remote_dir: str, files: list, *, dry_run: bool):
        installs.append((remote_dir, [n for n, _, _ in files]))
        return 0

    def fake_remote(ssh: str, cmd: str, *, dry_run: bool = False):
        remote_cmds.append(cmd)
        return 0

    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.s3_cli.install_files_via_ssh",
        fake_install,
    )
    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.s3_cli.remote_run",
        fake_remote,
    )
    install_garage_s3_cli(
        "root@203.0.113.28",
        cfg,
        access_key_id="GKabc",
        secret_access_key="secret",
        bucket="minimal-backups",
        region="garage",
        admin_token="tok",
        dry_run=False,
    )
    assert len(installs) == 2
    assert installs[0][0] == cfg.ops.install_prefix
    assert S3_SCRIPT_NAME in installs[0][1]
    assert ADMIN_SCRIPT_NAME in installs[0][1]
    assert installs[1][0] == cfg.ops.config_dir
    assert ENV_FILENAME in installs[1][1]
    assert any("/usr/local/bin" in c and "ln -sfn" in c for c in remote_cmds)
