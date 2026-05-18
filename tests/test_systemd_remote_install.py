"""Tests for catalpa_tooling.systemd_remote_install."""

import pytest

from catalpa_tooling.systemd_remote_install import (
    parse_docker_host_to_ssh_target,
    parse_install_systemd_flags,
    redact_env_file_content,
    render_restic_env,
)


def test_parse_docker_host_ssh_url() -> None:
    assert parse_docker_host_to_ssh_target("ssh://root@staging.example.com") == "root@staging.example.com"


def test_parse_docker_host_user_at_host() -> None:
    assert parse_docker_host_to_ssh_target("deploy@10.0.0.5") == "deploy@10.0.0.5"


def test_parse_docker_host_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_docker_host_to_ssh_target("")


def test_parse_docker_host_no_user() -> None:
    with pytest.raises(ValueError, match="no SSH user"):
        parse_docker_host_to_ssh_target("ssh://hostonly.example")


def test_parse_install_flags() -> None:
    assert parse_install_systemd_flags([]) == (False, False, None)
    assert parse_install_systemd_flags(["--dry-run", "--enable"]) == (True, True, None)
    assert parse_install_systemd_flags(["--only", "restic"]) == (False, False, "restic")


def test_parse_install_flags_fixed_only() -> None:
    assert parse_install_systemd_flags([], fixed_only="pgbackrest") == (False, False, "pgbackrest")
    assert parse_install_systemd_flags(["--dry-run"], fixed_only="restic") == (True, False, "restic")


def test_parse_install_flags_rejects_only_with_fixed() -> None:
    with pytest.raises(ValueError, match="not valid"):
        parse_install_systemd_flags(["--only", "restic"], fixed_only="pgbackrest")


def test_render_restic_env_includes_aws_from_s3_keys() -> None:
    body = render_restic_env(
        {
            "RESTIC_REPOSITORY": "s3:s3.amazonaws.com/bucket",
            "RESTIC_PASSWORD": "pw",
            "RESTIC_S3_ACCESS_KEY_ID": "AKIA",
            "RESTIC_S3_SECRET_ACCESS_KEY": "secret",
            "RESTIC_S3_DEFAULT_REGION": "us-east-1",
        }
    )
    assert "AWS_ACCESS_KEY_ID=AKIA" in body
    assert "AWS_SECRET_ACCESS_KEY=secret" in body
    assert "AWS_DEFAULT_REGION=us-east-1" in body


def test_redact_env_file_content() -> None:
    raw = "RESTIC_REPOSITORY=s3:bucket/x\nRESTIC_PASSWORD=secret\nCOMPOSE_PROJECT_NAME=pas_indmo\n"
    out = redact_env_file_content(raw)
    assert "secret" not in out
    assert "<redacted>" in out
    assert "pas_indmo" in out
