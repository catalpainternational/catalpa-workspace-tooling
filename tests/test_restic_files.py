"""Tests for restic docker env passthrough."""

import pytest

from catalpa_tooling.config import ProjectConfigError, load_project_config
from catalpa_tooling.restic_files import (
    _docker_run_env_flags,
    _docker_run_restic,
    _restic_verbose_flags,
    django_media_volume_name,
    merge_restic_verbose_from_cli,
    normalize_restic_credentials,
    restic_backup_mount_path,
    restic_backup_targets,
    restic_credentials_conflict_message,
    restic_env_for_restore,
    run_backup,
    run_restore,
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


def test_restic_backup_mount_path_custom_backup_path(minimal_project, tmp_path) -> None:
    tooling = minimal_project.tooling_path
    text = tooling.read_text(encoding="utf-8")
    tooling.write_text(
        text.replace(
            "  zabbix:",
            "  restic:\n    backup_path: /mnt/btrfs-data/jid-media\n  zabbix:",
        ),
        encoding="utf-8",
    )
    cfg = load_project_config(minimal_project.repo_root)
    assert restic_backup_mount_path(config=cfg) == "/mnt/btrfs-data/jid-media"
    body = render_restic_env({}, config=cfg)
    assert "RESTIC_FILES_BACKUP_PATH=/mnt/btrfs-data/jid-media" in body


def _with_restic(minimal_project, block: str):
    """Rewrite the fixture's ``ops.restic`` block and reload the config."""
    tooling = minimal_project.tooling_path
    text = tooling.read_text(encoding="utf-8")
    tooling.write_text(text.replace("  zabbix:", f"  restic:\n{block}  zabbix:"), encoding="utf-8")
    return load_project_config(minimal_project.repo_root)


_THREE_VOLUMES = (
    "    data_volumes:\n"
    "      - django_media\n"
    "      - django_public_documents\n"
    "      - django_private_uploads\n"
)


def test_data_volumes_list_sets_primary_and_extras(minimal_project) -> None:
    cfg = _with_restic(minimal_project, _THREE_VOLUMES + "      - django_media\n")
    assert cfg.ops.restic.data_volume == "django_media"
    assert cfg.ops.restic.extra_volumes == ("django_public_documents", "django_private_uploads")
    assert restic_backup_targets("prod", config=cfg) == [
        ("django_media", "prod_django_media", "/backup/django_media"),
        ("django_public_documents", "prod_django_public_documents", "/backup/django_public_documents"),
        ("django_private_uploads", "prod_django_private_uploads", "/backup/django_private_uploads"),
    ]
    body = render_restic_env({}, config=cfg)
    assert "RESTIC_FILES_DATA_VOLUME=django_media" in body
    assert "RESTIC_FILES_EXTRA_VOLUMES=django_public_documents django_private_uploads" in body


def test_single_data_volume_has_no_extras(minimal_project) -> None:
    assert minimal_project.ops.restic.data_volumes == ("django_media",)
    assert "RESTIC_FILES_EXTRA_VOLUMES" not in render_restic_env({}, config=minimal_project)


@pytest.mark.parametrize(
    ("block", "match"),
    [
        ("    data_volume: x\n    data_volumes: [y]\n", "not both"),
        ("    data_volumes: []\n", "must not be empty"),
        ("    data_volumes: [ok, 'bad key']\n", "ops.restic.data_volumes"),
    ],
)
def test_data_volumes_rejects_bad_config(minimal_project, block: str, match: str) -> None:
    with pytest.raises(ProjectConfigError, match=match):
        _with_restic(minimal_project, block)


def _record_docker(monkeypatch: pytest.MonkeyPatch, *, existing: set[str]) -> list[list[str]]:
    calls: list[list[str]] = []

    class _R:
        def __init__(self, rc: int) -> None:
            self.returncode = rc
            self.stdout = self.stderr = ""

    def fake_run(cmd: list[str], **kwargs: object) -> _R:
        calls.append(cmd)
        if cmd[:3] == ["docker", "volume", "inspect"]:
            return _R(0 if cmd[3] in existing else 1)
        return _R(0)

    monkeypatch.setattr("catalpa_tooling.restic_files.run_cmd", fake_run)
    return calls


_WRITE_ENV = {
    "COMPOSE_PROJECT_NAME": "prod",
    "RESTIC_WRITE_REPOSITORY": "s3:bucket/x",
    "RESTIC_WRITE_PASSWORD": "p",
}


def test_run_backup_takes_every_volume_in_one_snapshot(
    minimal_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _with_restic(minimal_project, _THREE_VOLUMES)
    calls = _record_docker(
        monkeypatch,
        existing={"prod_django_public_documents", "prod_django_private_uploads"},
    )
    assert run_backup(dict(_WRITE_ENV), config=cfg) == 0
    (run,) = [c for c in calls if c[:2] == ["docker", "run"]]
    assert "prod_django_private_uploads:/backup/django_private_uploads:ro" in run
    assert run[run.index("backup") :] == [
        "backup",
        "/backup/django_media",
        "/backup/django_public_documents",
        "/backup/django_private_uploads",
    ]


def test_run_backup_skips_a_missing_extra_volume_and_fails(
    minimal_project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`docker run -v` would create the volume empty and back that up, hiding the gap."""
    cfg = _with_restic(minimal_project, _THREE_VOLUMES)
    calls = _record_docker(monkeypatch, existing={"prod_django_public_documents"})
    assert run_backup(dict(_WRITE_ENV), config=cfg) == 1
    (run,) = [c for c in calls if c[:2] == ["docker", "run"]]
    assert not any("private_uploads" in arg for arg in run)
    assert "/backup/django_public_documents" in run
    assert "prod_django_private_uploads" in capsys.readouterr().err


def test_run_restore_promotes_each_volume(
    minimal_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _with_restic(minimal_project, _THREE_VOLUMES)
    calls = _record_docker(monkeypatch, existing=set())
    assert run_restore(dict(_WRITE_ENV), "latest", env_name="prod", skip_confirm=True, config=cfg) == 0

    restic_run, *alpine_runs = [c for c in calls if c[:2] == ["docker", "run"]]
    # On a rootless SELinux host restic could not set the source's labels on any file.
    i = restic_run.index("--exclude-xattr")
    assert restic_run[i + 1] == "security.selinux"
    # `--path` names the primary only, so snapshots from before the extras still match.
    assert restic_run[restic_run.index("--path") :] == ["--path", "/backup/django_media"]
    assert [r[r.index("-v") + 1] for r in alpine_runs] == [
        "prod_django_media:/live",
        "prod_django_public_documents:/live",
        "prod_django_private_uploads:/live",
    ]
    primary, extra, _ = (r[-1] for r in alpine_runs)
    assert "exit 1" in primary and "/stage/backup/django_media" in primary
    # An older snapshot without this tree leaves the volume alone instead of emptying it.
    assert "leaving volume django_public_documents unchanged" in extra
    assert extra.index("[ ! -d") < extra.index("rm -rf")


def test_backup_script_mounts_extra_volumes(tmp_path) -> None:
    """Run the systemd script itself against a stub `docker` that logs its argv."""
    import os
    import subprocess

    from catalpa_tooling.systemd_assets import systemd_source_dir

    log = tmp_path / "docker.log"
    stub = tmp_path / "bin" / "docker"
    stub.parent.mkdir()
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> {log}\n'
        'if [[ "$1 $2" == "volume inspect" && "$3" == *missing* ]]; then exit 1; fi\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    script = systemd_source_dir() / "restic-files-backup.sh"
    env = {
        "PATH": f"{stub.parent}:{os.environ['PATH']}",
        "COMPOSE_PROJECT_NAME": "prod",
        "RESTIC_REPOSITORY": "s3:bucket/x",
        "RESTIC_PASSWORD": "p",
        "RESTIC_FILES_DATA_VOLUME": "django_media",
        "RESTIC_FILES_EXTRA_VOLUMES": "django_private_uploads missing_vol",
    }
    r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert r.returncode == 1, r.stderr
    assert "prod_missing_vol" in r.stderr
    run_line = [line for line in log.read_text().splitlines() if line.startswith("run ")][0]
    assert "-v prod_django_private_uploads:/backup/django_private_uploads:ro" in run_line
    assert "missing_vol" not in run_line
    assert run_line.endswith("backup /backup/django_media /backup/django_private_uploads")

    env.pop("RESTIC_FILES_EXTRA_VOLUMES")
    log.unlink()
    r = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert log.read_text().strip().endswith("backup /backup/django_media")
