"""Tests for DigitalOcean ↔ dk environment linking."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from catalpa_tooling.deploy_do_link import (
    EnvDoLink,
    format_docker_host,
    patch_info_docker_host,
    public_ipv4,
    read_env_do_link,
)
from catalpa_tooling.doctl_projects import _droplet_row


def test_public_ipv4() -> None:
    droplet = {
        "networks": {
            "v4": [
                {"type": "private", "ip_address": "10.0.0.1"},
                {"type": "public", "ip_address": "203.0.113.5"},
            ]
        }
    }
    assert public_ipv4(droplet) == "203.0.113.5"


def test_read_env_do_link() -> None:
    assert read_env_do_link({}) is None
    assert read_env_do_link({"digitalocean": {}}) is None
    link = read_env_do_link({"digitalocean": {"droplet_name": "web-1", "ssh_user": "deploy"}})
    assert link == EnvDoLink(droplet_name="web-1", ssh_user="deploy")


def test_format_docker_host() -> None:
    assert format_docker_host("root", "1.2.3.4") == "ssh://root@1.2.3.4"
    assert format_docker_host("", "1.2.3.4") == "ssh://root@1.2.3.4"


def test_droplet_row_env_column() -> None:
    droplet = {
        "id": 1,
        "name": "marktwain-prod",
        "status": "active",
        "region": {"slug": "sgp1"},
        "networks": {"v4": [{"type": "public", "ip_address": "1.2.3.4"}]},
    }
    row = _droplet_row(
        droplet,
        ("Name", "Env"),
        env_by_droplet_name={"marktwain-prod": "prod"},
    )
    assert row == ["marktwain-prod", "prod"]


def test_patch_info_docker_host(tmp_path: Path) -> None:
    info_path = tmp_path / "info.yaml"
    info_path.write_text("site_origin: example.com\n", encoding="utf-8")
    assert patch_info_docker_host(info_path, "ssh://root@1.2.3.4", dry_run=True) == 0
    data = yaml.safe_load(info_path.read_text(encoding="utf-8"))
    assert "docker_host" not in data

    assert patch_info_docker_host(info_path, "ssh://root@1.2.3.4", dry_run=False) == 0
    data = yaml.safe_load(info_path.read_text(encoding="utf-8"))
    assert data["docker_host"] == "ssh://root@1.2.3.4"


def test_cmd_env_host_missing_link(tmp_path: Path) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host

    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("description: test\n", encoding="utf-8")

    tooling = tmp_path / "tooling.yaml"
    tooling.write_text(
        """
project:
  name: test
  root_marker: tooling.yaml
paths:
  backend: django
  frontend: django
  scripts: scripts
  env_local: .env.local
  email_backend_dir: django/var/email_out
  fetch_db_dump: dump
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yaml
    dev_compose: compose.dev.yaml
    credentials_optional_envs: []
stack:
  compose_project_default: tempu
  services:
    web: django
    proxy: caddy
    db: db
  images:
    registry_key: image_registry
    components:
      web: app-web
      proxy: app-caddy
      db: app-db
  healthcheck:
    service: django
    url: http://localhost:8000/
ops:
  install_prefix: /opt/app
  config_dir: /etc/app
  systemd_unit_prefix: app-
  transfer_workdir: .transfer
  default_db_container: db-1
  pgbackrest:
    postgres_conf: pg.conf
    pgbackrest_conf: pgb.conf
    default_registry: ghcr.io/example
    restore_temp_prefix: restore_
  zabbix:
    unit_name: zabbix.service
    userparams_file: zabbix.conf
  systemd_units:
    pgbackrest: []
    restic: []
    timers_enable_pgbackrest: []
    timers_enable_restic: []
""",
        encoding="utf-8",
    )

    from catalpa_tooling.config import load_project_config

    config = load_project_config(tmp_path)
    assert cmd_env_host(config, "prod", write=False, dry_run=True) == 1


def test_find_droplet_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from catalpa_tooling.deploy_do_link import find_droplet_by_name

    def fake_json(args: list[str], *, context: str | None) -> object:
        return [
            {"id": 1, "name": "alpha"},
            {"id": 2, "name": "marktwain-prod"},
        ]

    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl_json", fake_json)
    found = find_droplet_by_name("marktwain-prod", context=None)
    assert found is not None
    assert found["id"] == 2
    assert find_droplet_by_name("missing", context=None) is None
