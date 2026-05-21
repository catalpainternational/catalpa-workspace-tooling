"""Tests for restic docker env passthrough."""

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.restic_files import (
    _docker_run_env_flags,
    _docker_run_restic,
    _restic_verbose_flags,
    django_media_volume_name,
    merge_restic_verbose_from_cli,
    normalize_restic_credentials,
    restic_backup_mount_path,
    restic_credentials_conflict_message,
    restic_env_for_restore,
    split_restic_cli_verbose,
    validate_restic_env,
    validate_restic_env_for_systemd,
)
from catalpa_tooling.systemd_remote_install import render_restic_env


def test_docker_run_env_flags_passes_repo_and_password() -> None:
    merged = {
        "RESTIC_REPOSITORY": "s3:s3.amazonaws.com/bucket/prefix",
        "RESTIC_PASSWORD": "secret",
    }
    flags = _docker_run_env_flags(merged)
    assert flags == [
        "-e",
        "RESTIC_REPOSITORY",
        "-e",
        "RESTIC_PASSWORD",
    ]


def test_split_restic_cli_verbose() -> None:
    assert split_restic_cli_verbose(["init", "-v", "-v"]) == (["init"], 2)
    assert split_restic_cli_verbose(["restore", "latest", "-v"]) == (["restore", "latest"], 1)


def test_merge_restic_verbose_from_cli() -> None:
    base = {"RESTIC_VERBOSE": "1"}
    m = merge_restic_verbose_from_cli(base, 2)
    assert m["RESTIC_VERBOSE"] == "3"


def test_restic_env_for_restore_defaults() -> None:
    out = restic_env_for_restore({})
    assert out["RESTIC_VERBOSE"] == "1"
    assert _restic_verbose_flags(out) == ["-v"]


def test_restic_env_for_restore_restore_override() -> None:
    out = restic_env_for_restore({"RESTIC_VERBOSE": "2", "RESTIC_RESTORE_VERBOSE": "3"})
    assert out["RESTIC_VERBOSE"] == "3"


def test_restic_env_for_restore_inherits_verbose() -> None:
    out = restic_env_for_restore({"RESTIC_VERBOSE": "2"})
    assert out["RESTIC_VERBOSE"] == "2"


def test_restic_env_for_restore_quiet() -> None:
    out = restic_env_for_restore({"RESTIC_RESTORE_VERBOSE": "0"})
    assert out["RESTIC_VERBOSE"] == "0"
    assert _restic_verbose_flags(out) == []


def test_docker_run_env_flags_includes_aws_when_set() -> None:
    merged = {
        "RESTIC_REPOSITORY": "s3:bucket/x",
        "RESTIC_PASSWORD": "p",
        "AWS_ACCESS_KEY_ID": "AKIA",
        "AWS_SECRET_ACCESS_KEY": "sk",
        "AWS_DEFAULT_REGION": "ap-southeast-2",
    }
    flags = _docker_run_env_flags(merged)
    assert flags.count("-e") == 5
    assert "AWS_DEFAULT_REGION" in flags


def test_normalize_restic_write_prefix() -> None:
    n = normalize_restic_credentials(
        {
            "RESTIC_WRITE_REPOSITORY": "s3:bucket/p",
            "RESTIC_WRITE_PASSWORD": "pw",
            "RESTIC_WRITE_S3_ACCESS_KEY_ID": "k",
        }
    )
    assert n["RESTIC_REPOSITORY"] == "s3:bucket/p"
    assert n["RESTIC_PASSWORD"] == "pw"
    assert n["RESTIC_S3_ACCESS_KEY_ID"] == "k"


def test_normalize_restic_read_prefix() -> None:
    n = normalize_restic_credentials(
        {
            "RESTIC_READ_REPOSITORY": "s3:bucket/r",
            "RESTIC_READ_PASSWORD": "rp",
        }
    )
    assert n["RESTIC_REPOSITORY"] == "s3:bucket/r"
    assert n["RESTIC_PASSWORD"] == "rp"


def test_restic_conflict_legacy_and_read() -> None:
    msg = restic_credentials_conflict_message(
        {
            "RESTIC_REPOSITORY": "s3:a",
            "RESTIC_PASSWORD": "p",
            "RESTIC_READ_REPOSITORY": "s3:b",
        }
    )
    assert msg is not None
    assert "mutually exclusive" in msg


def test_validate_systemd_rejects_read_only() -> None:
    err = validate_restic_env_for_systemd(
        {
            "RESTIC_READ_REPOSITORY": "s3:bucket/x",
            "RESTIC_READ_PASSWORD": "p",
        }
    )
    assert err is not None
    assert "systemd" in err.lower() or "RESTIC_READ" in err


def test_validate_restic_read_ok() -> None:
    assert (
        validate_restic_env(
            {
                "RESTIC_READ_REPOSITORY": "s3:bucket/x",
                "RESTIC_READ_PASSWORD": "p",
            }
        )
        is None
    )


class _FakeResult:
    returncode = 0


def test_docker_run_restic_prepends_no_lock_for_read_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> _FakeResult:
        captured.append(cmd)
        return _FakeResult()

    monkeypatch.setattr("catalpa_tooling.restic_files.run_cmd", fake_run)
    env = {
        "RESTIC_READ_REPOSITORY": "s3:bucket/x",
        "RESTIC_READ_PASSWORD": "p",
    }
    rc = _docker_run_restic(env, [], ["snapshots"])
    assert rc == 0
    assert captured
    cmd = captured[0]
    assert "--no-lock" in cmd
    assert cmd.index("--no-lock") < cmd.index("snapshots")


def test_docker_run_restic_no_extra_lock_flag_for_write_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> _FakeResult:
        captured.append(cmd)
        return _FakeResult()

    monkeypatch.setattr("catalpa_tooling.restic_files.run_cmd", fake_run)
    env = {
        "RESTIC_WRITE_REPOSITORY": "s3:bucket/x",
        "RESTIC_WRITE_PASSWORD": "p",
    }
    rc = _docker_run_restic(env, [], ["snapshots"])
    assert rc == 0
    assert captured
    assert "--no-lock" not in captured[0]


def test_render_restic_env_from_write_prefix() -> None:
    body = render_restic_env(
        {
            "COMPOSE_PROJECT_NAME": "pas_indmo",
            "RESTIC_WRITE_REPOSITORY": "s3:bucket/x",
            "RESTIC_WRITE_PASSWORD": "secret",
        }
    )
    assert "RESTIC_REPOSITORY=s3:bucket/x" in body
    assert "RESTIC_PASSWORD=secret" in body
    assert "RESTIC_FILES_DATA_VOLUME=django_media" in body


def test_django_media_volume_name_default(minimal_project) -> None:
    assert django_media_volume_name("myproj", config=minimal_project) == "myproj_django_media"
    assert restic_backup_mount_path(config=minimal_project) == "/backup/django_media"


def test_django_media_volume_name_custom_data_volume(minimal_project, tmp_path) -> None:
    tooling = minimal_project.tooling_path
    text = tooling.read_text(encoding="utf-8")
    tooling.write_text(
        text.replace(
            "  zabbix:",
            "  restic:\n    data_volume: user_uploads\n  zabbix:",
        ),
        encoding="utf-8",
    )
    cfg = load_project_config(minimal_project.repo_root)
    assert django_media_volume_name("prod", config=cfg) == "prod_user_uploads"
    assert restic_backup_mount_path(config=cfg) == "/backup/user_uploads"
    body = render_restic_env({}, config=cfg)
    assert "RESTIC_FILES_DATA_VOLUME=user_uploads" in body
