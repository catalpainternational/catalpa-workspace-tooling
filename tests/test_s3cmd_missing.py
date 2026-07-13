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
    from catalpa_tooling.config import (
        DEFAULT_BUILD_PLACEHOLDERS,
        DEFAULT_ORIGIN_ENV_KEYS,
        DEFAULT_BUILD_TIME_ZONE,
        DEFAULT_DEV_LAN_DNS_SUFFIX,
        DEFAULT_DEV_SITE_ORIGIN_BASE,
        DevConfig,
        DeployPathsConfig,
        FetchMetabaseDbConfig,
        FetchConfig,
        FetchDatabaseEntry,
        DjangoDevConfig,
        NativeConfig,
        FetchMediaConfig,
        FrontendDevConfig,
        ResetDbConfig,
        StartConfig,
        OpsConfig,
        PathsConfig,
        PgbackrestOpsConfig,
        PostDbRestoreOpsConfig,
        PostMetabaseDbRestoreOpsConfig,
        ProjectConfig,
        ProjectMetaConfig,
        ResticOpsConfig,
        StackConfig,
        StackHealthcheckConfig,
        StackImagesConfig,
        StackServicesConfig,
        SystemdUnitsOpsConfig,
        ZabbixOpsConfig,
    )

    creds = tmp_path / "credentials.yaml"
    creds.write_text("sops: {}\n", encoding="utf-8")
    cfg = ProjectConfig(
        meta=ProjectMetaConfig(name="app", root_marker="pyproject.toml"),
        paths=PathsConfig(
            backend="b",
            frontend="f",
            prototype=None,
            scripts=("s",),
            env_local=".env",
            email_backend_dir="e",
            media_dir=None,
            fetch_db_dump="d",
            fetch_metabase_db_dump=None,
            deploy=DeployPathsConfig(
                envs_dir="docker/envs",
                images_config="i.yaml",
                default_compose="c.yml",
                dev_compose="d.yml",
                credentials_optional_envs=(),
                env_aliases={},
            ),
        ),
        stack=StackConfig(
            compose_project_default="app",
            services=StackServicesConfig(web="w", proxy="p", db="db"),
            images=StackImagesConfig(registry_key="r", components={"web": "w", "proxy": "p", "db": "db"}),
            healthcheck=StackHealthcheckConfig(service="w", url="http://localhost/"),
            origin_env_keys=DEFAULT_ORIGIN_ENV_KEYS,
            build_placeholders=dict(DEFAULT_BUILD_PLACEHOLDERS),
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
                pg1_path="/var/lib/postgresql/18/docker",
            ),
            restic=ResticOpsConfig(data_volume="media"),
            zabbix=ZabbixOpsConfig(unit_name="z.service", userparams_file="z.conf"),
            systemd_units=SystemdUnitsOpsConfig(
                pgbackrest=("s.service",),
                restic=("r.service",),
                timers_enable_pgbackrest=(),
                timers_enable_restic=(),
            ),
            post_db_restore=PostDbRestoreOpsConfig(envs=None, db_psql=(), manage_commands=()),
            post_metabase_db_restore=PostMetabaseDbRestoreOpsConfig(
                envs=None,
                db_psql=(),
                manage_commands=(),
                restart_services=(),
            ),
            default_db_container="db1",
        ),
        native=NativeConfig(
            fetch=FetchConfig(
                dk_env="prod",
                ssh_host=None,
                databases={
                    "app": FetchDatabaseEntry(db_name="d", via="ssh_native"),
                },
            ),
            fetch_media=FetchMediaConfig(dk_env="prod", dest="media", legacy=None),
            fetch_metabase_db=FetchMetabaseDbConfig(ssh_host=None),
            reset_db=ResetDbConfig(
                postgis=False,
                restore_as_super=False,
                pg_restore_args=("--clean", "--if-exists"),
                post_manage_commands=(),
                db_name_env=("POSTGRES_DB",),
                db_name_fallback=None,
                host_env=("POSTGRES_HOST",),
                port_env=("POSTGRES_PORT",),
                user_env=("POSTGRES_USER",),
                password_env=("POSTGRES_PASSWORD",),
            ),
            django=DjangoDevConfig(port=None),
            frontend=FrontendDevConfig(
                package_manager=None,
                script="dev",
                install=True,
                env={},
                node_version=None,
            ),
            start=StartConfig(
                procfile=None,
                ports=(8000, 8080),
                migrate=True,
            ),
        ),
        dev=DevConfig(
            site_origin_base=DEFAULT_DEV_SITE_ORIGIN_BASE,
            lan_dns_suffix=DEFAULT_DEV_LAN_DNS_SUFFIX,
            build_time_zone=DEFAULT_BUILD_TIME_ZONE,
        ),
        digitalocean=None,
        compliance=None,
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
