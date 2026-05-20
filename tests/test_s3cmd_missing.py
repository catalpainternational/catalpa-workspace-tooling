"""Graceful behaviour when host ``s3cmd`` is not installed."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalpa_tooling.doctl_spaces_provision import ensure_spaces_backup_credentials
from catalpa_tooling.s3cmd_binary import S3cmdNotFoundError, resolve_s3cmd_binary


def test_resolve_s3cmd_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("S3CMD_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    with pytest.raises(S3cmdNotFoundError, match="Install"):
        resolve_s3cmd_binary()


def test_provision_exits_when_s3cmd_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    from catalpa_tooling.config import ProjectConfig, ProjectMetaConfig, PathsConfig, DeployPathsConfig, StackConfig, StackServicesConfig, StackImagesConfig, StackHealthcheckConfig, OpsConfig, PgbackrestOpsConfig, ResticOpsConfig, ZabbixOpsConfig, SystemdUnitsOpsConfig

    creds = tmp_path / "credentials.yaml"
    creds.write_text("sops: {}\n", encoding="utf-8")
    cfg = ProjectConfig(
        meta=ProjectMetaConfig(name="app", root_marker="pyproject.toml"),
        paths=PathsConfig(
            backend="b",
            frontend="f",
            prototype=None,
            scripts="s",
            env_local=".env",
            email_backend_dir="e",
            fetch_db_dump="d",
            deploy=DeployPathsConfig(
                envs_dir="docker/envs",
                images_config="i.yaml",
                default_compose="c.yml",
                dev_compose="d.yml",
                credentials_optional_envs=(),
            ),
        ),
        stack=StackConfig(
            compose_project_default="app",
            services=StackServicesConfig(web="w", proxy="p", db="db"),
            images=StackImagesConfig(registry_key="r", components={"web": "w", "proxy": "p", "db": "db"}),
            healthcheck=StackHealthcheckConfig(service="w", url="http://localhost/"),
        ),
        ops=OpsConfig(
            install_prefix="/opt",
            config_dir="/etc",
            systemd_unit_prefix="a-",
            transfer_workdir=".t",
            pgbackrest=PgbackrestOpsConfig(
                postgres_conf="a.conf",
                pgbackrest_conf="b.conf",
                default_registry="ghcr.io/x",
                restore_temp_prefix="p_",
                data_volume="db_data",
            ),
            restic=ResticOpsConfig(data_volume="media"),
            zabbix=ZabbixOpsConfig(unit_name="z.service", userparams_file="z.conf"),
            systemd_units=SystemdUnitsOpsConfig(
                pgbackrest=("s.service",),
                restic=("r.service",),
                timers_enable_pgbackrest=(),
                timers_enable_restic=(),
            ),
            default_db_container="db1",
        ),
        digitalocean=None,
        repo_root=tmp_path,
        tooling_path=tmp_path / "tooling.yaml",
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_spaces_provision.ensure_doctl_available",
        lambda: Path("/usr/bin/doctl"),
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_spaces_provision.ensure_s3cmd_available",
        lambda: (_ for _ in ()).throw(S3cmdNotFoundError("missing")),
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_spaces_provision.confirm_yes_default_no",
        lambda _prompt: True,
    )
    env: dict[str, str] = {}
    rc = ensure_spaces_backup_credentials(
        cfg,
        "prod",
        env,
        creds,
        target="pgbackrest",
        command_label="dk prod bkp_db backup",
        yes=False,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "s3cmd" in err
