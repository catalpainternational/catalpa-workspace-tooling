"""Tests for catalpa_tooling.fetch_media_config."""

from pathlib import Path

import yaml

from catalpa_tooling.config import load_project_config
from catalpa_tooling.fetch_media_config import (
    build_fetch_media_env,
    dk_info_fetch_media_defaults,
)


def _write_minimal_tooling(tmp_path: Path, *, compose_default: str = "app_compose") -> None:
    (tmp_path / "pyproject.toml").write_text("n=1\n", encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(
        f"""
project:
  name: test
paths:
  backend: backend
  frontend: frontend
  scripts: scripts
  env_local: .env.local
  email_backend_dir: email_out
  fetch_db_dump: dump
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
stack:
  compose_project_default: {compose_default}
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
""",
        encoding="utf-8",
    )


def test_dk_info_fetch_media_defaults(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "staging"
    env_dir.mkdir(parents=True)
    info = {
        "docker_host": "ssh://deploy@host.example",
        "env": {"compose_project_name": "pas_indmo_staging"},
    }
    (env_dir / "info.yaml").write_text(yaml.dump(info), encoding="utf-8")
    cfg = load_project_config(tmp_path)
    ssh, project = dk_info_fetch_media_defaults(cfg, "staging")
    assert ssh == "deploy@host.example"
    assert project == "pas_indmo_staging"


def test_build_fetch_media_env_docker_volume(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "staging"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        yaml.dump({"docker_host": "ssh://u@h", "env": {}}),
        encoding="utf-8",
    )
    env = build_fetch_media_env(
        cfg,
        legacy_path=False,
        dk_env="staging",
        host=None,
        remote_path="",
        dest=tmp_path / "media",
        partial=False,
        compose_project=None,
    )
    assert env["FETCH_MEDIA_SOURCE"] == "docker_volume"
    assert env["FETCH_MEDIA_SSH_HOST"] == "u@h"
    assert env["FETCH_COMPOSE_PROJECT_NAME"] == "app_compose"


def test_build_fetch_media_env_legacy_requires_host(tmp_path: Path) -> None:
    _write_minimal_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    try:
        build_fetch_media_env(
            cfg,
            legacy_path=True,
            dk_env="staging",
            host=None,
            remote_path="/backup/media",
            dest=None,
            partial=False,
            compose_project=None,
        )
    except ValueError as exc:
        assert "host" in str(exc).lower()
    else:
        raise AssertionError("expected ValueError")
