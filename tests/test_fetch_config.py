"""Tests for native.fetch.databases config parsing."""

from pathlib import Path

import pytest
import yaml

from catalpa_tooling.config import (
    FetchDatabaseEntry,
    ProjectConfigError,
    load_project_config,
)


def _write_minimal_tooling(tmp_path: Path, *, extra: str = "", omit_metabase_path: bool = False) -> None:
    (tmp_path / "pyproject.toml").write_text("n=1\n", encoding="utf-8")
    mb_line = (
        ""
        if omit_metabase_path
        else "  fetch_metabase_db_dump: docker/dumps/metabase_db.custom\n"
    )
    (tmp_path / "tooling.yaml").write_text(
        f"""
project:
  name: testapp
paths:
  backend: backend
  frontend: frontend
  scripts: scripts
  env_local: .env.local
  email_backend_dir: email_out
  fetch_db_dump: docker/dumps/app_db.custom
{mb_line}  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
stack:
  compose_project_default: testapp
  services: {{web: w, proxy: p, db: d}}
  images:
    registry_key: image_registry
    components: {{web: w-img, proxy: p-img, db: d-img}}
  healthcheck: {{service: w, url: http://localhost/healthz}}
ops:
  install_prefix: /opt/app
  config_dir: /etc/app
  systemd_unit_prefix: app-
  transfer_workdir: .xfer
  default_db_container: app_db
  pgbackrest:
    postgres_conf: a.conf
    pgbackrest_conf: b.conf
    default_registry: reg
    restore_temp_prefix: pre_
  zabbix:
    unit_name: u.service
    userparams_file: 99.conf
  systemd_units:
    pgbackrest: []
    restic: []
{extra}
""",
        encoding="utf-8",
    )


def test_parse_explicit_fetch_databases(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(
        tmp_path,
        extra="""
native:
  fetch:
    dk_env: prod
    ssh_host: root@example.com
    databases:
      app:
        db_name: bero_db
        via: ssh_native
      metabase:
        db_name: metabase_db
        via: ssh_native
""",
    )
    cfg = load_project_config(tmp_path)
    assert cfg.default_fetch_dk_env == "prod"
    assert cfg.native.fetch.ssh_host == "root@example.com"
    assert cfg.native.fetch.databases["app"] == FetchDatabaseEntry(
        db_name="bero_db",
        via="ssh_native",
        ssh_host=None,
    )
    assert cfg.has_metabase_fetch() is True
    assert cfg.fetch_database_output_path("app", cfg.native.fetch.databases["app"]) == (
        tmp_path / "docker/dumps/app_db.custom"
    )


def test_ssh_docker_requires_container(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(
        tmp_path,
        extra="""
native:
  fetch:
    databases:
      app:
        db_name: ncd_db
        via: ssh_docker
""",
    )
    with pytest.raises(ProjectConfigError, match="container is required"):
        load_project_config(tmp_path)


def test_legacy_fetch_from_metabase_ssh_host(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(
        tmp_path,
        extra="""
native:
  fetch_metabase_db:
    ssh_host: root@legacy.example
""",
    )
    cfg = load_project_config(tmp_path)
    assert cfg.native.fetch.databases["app"].db_name == "app_db"
    assert cfg.native.fetch.databases["app"].via == "ssh_native"
    assert cfg.native.fetch.ssh_host == "root@legacy.example"
    assert cfg.native.fetch.databases["metabase"] == FetchDatabaseEntry(
        db_name="metabase_db",
        via="ssh_native",
        ssh_host="root@legacy.example",
        soft_skip_if_missing=True,
    )


def test_legacy_script_via_when_fetch_db_sh_exists(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "fetch_db.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    cfg = load_project_config(tmp_path)
    assert cfg.native.fetch.databases["app"].via == "script"


def test_default_fetch_via_dk_when_prod_has_docker_host(
    tmp_path: Path, isolated_tooling: None
) -> None:
    _write_minimal_tooling(tmp_path, omit_metabase_path=True)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        yaml.dump({"docker_host": "ssh://root@206.189.81.205", "env": {}}),
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    assert cfg.native.fetch.dk_env == "prod"
    assert cfg.native.fetch.databases["app"] == FetchDatabaseEntry(
        db_name="app_db",
        via="dk",
    )
    assert cfg.native.fetch.databases["metabase"] == FetchDatabaseEntry(
        db_name="metabase_db",
        via="dk",
        soft_skip_if_missing=True,
    )
    assert cfg.fetch_metabase_db_dump_path == tmp_path / "docker/dumps/metabase_db.custom"
    assert cfg.has_metabase_fetch() is True


def test_default_fetch_respects_explicit_dk_env(
    tmp_path: Path, isolated_tooling: None
) -> None:
    _write_minimal_tooling(
        tmp_path,
        extra="""
native:
  fetch:
    dk_env: staging
""",
    )
    env_dir = tmp_path / "docker" / "envs" / "staging"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        yaml.dump({"docker_host": "ssh://root@staging.example", "env": {}}),
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    assert cfg.native.fetch.dk_env == "staging"
    assert cfg.native.fetch.databases["app"].via == "dk"


def test_docker_host_preferred_over_fetch_db_sh(
    tmp_path: Path, isolated_tooling: None
) -> None:
    _write_minimal_tooling(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "fetch_db.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        yaml.dump({"docker_host": "ssh://root@prod.example", "env": {}}),
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    assert cfg.native.fetch.databases["app"].via == "dk"
