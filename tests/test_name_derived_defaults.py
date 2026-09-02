"""Name-derived tooling.yaml defaults for fully Docker-deployed consumers."""

from pathlib import Path

import pytest
import yaml

from catalpa_tooling.config import (
    DEFAULT_PGBR_CONF,
    DEFAULT_PGBR_POSTGRES_CONF,
    DEFAULT_PGBR_REGISTRY,
    load_project_config,
)


def _write_bero_style_minimal(tmp_path: Path, *, tooling: str, prod_info: dict | None = None) -> None:
    (tmp_path / "pyproject.toml").write_text("n=1\n", encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(tooling, encoding="utf-8")
    if prod_info is not None:
        env_dir = tmp_path / "docker" / "envs" / "prod"
        env_dir.mkdir(parents=True)
        (env_dir / "info.yaml").write_text(yaml.dump(prod_info), encoding="utf-8")


def test_name_derived_ops_stack_and_fetch_db_dump(tmp_path: Path, isolated_tooling: None) -> None:
    _write_bero_style_minimal(
        tmp_path,
        tooling="""
project:
  name: jid
paths:
  backend: .
  frontend: bero
  scripts: scripts
  env_local: .env.local
  email_backend_dir: media
  deploy: {}
stack:
  healthcheck:
    url: http://localhost:8000/cms/
ops:
  post_db_restore:
    envs: [dev]
    manage_commands:
      - [migrate]
""",
        prod_info={
            "docker_host": "ssh://root@example",
            "storage": {"volumes": {"django_media": {"path": "/mnt/jid-media"}}},
        },
    )
    cfg = load_project_config(tmp_path)
    assert cfg.paths.fetch_db_dump == "docker/postgres/dumps/jid_db.custom"
    assert cfg.paths.deploy.default_compose == "compose.yaml"
    assert cfg.stack.compose_project_default == "jid"
    assert cfg.stack.services.web == "django"
    assert cfg.stack.images.components == {
        "web": "jid-django",
        "proxy": "jid-caddy",
        "db": "jid-db",
    }
    assert cfg.stack.healthcheck.service == "django"
    assert cfg.ops.install_prefix == "/opt/jid"
    assert cfg.ops.config_dir == "/etc/jid"
    assert cfg.ops.systemd_unit_prefix == "jid-"
    assert cfg.ops.transfer_workdir == ".jid-transfer"
    assert cfg.ops.default_db_container == "jid-full-db-1"
    assert cfg.ops.pgbackrest.postgres_conf == DEFAULT_PGBR_POSTGRES_CONF
    assert cfg.ops.pgbackrest.pgbackrest_conf == DEFAULT_PGBR_CONF
    assert cfg.ops.pgbackrest.default_registry == DEFAULT_PGBR_REGISTRY
    assert cfg.ops.zabbix.unit_name == "jid-zabbix-agent2.service"
    assert cfg.ops.zabbix.userparams_file == "99-jid-userparams.conf"
    assert "jid-pgbackrest-backup-full.timer" in cfg.ops.systemd_units.pgbackrest
    assert "jid-restic-files-backup.timer" in cfg.ops.systemd_units.restic
    assert cfg.ops.restic.backup_path == "/mnt/jid-media"


def test_explicit_restic_backup_path_wins(tmp_path: Path, isolated_tooling: None) -> None:
    _write_bero_style_minimal(
        tmp_path,
        tooling="""
project:
  name: jid
paths:
  backend: .
  frontend: bero
  scripts: scripts
  env_local: .env.local
  email_backend_dir: media
  deploy: {}
stack:
  healthcheck:
    url: http://localhost:8000/cms/
ops:
  restic:
    backup_path: /mnt/btrfs-data/jid-media
""",
        prod_info={
            "storage": {"volumes": {"django_media": {"path": "/mnt/jid-media"}}},
        },
    )
    cfg = load_project_config(tmp_path)
    assert cfg.ops.restic.backup_path == "/mnt/btrfs-data/jid-media"


def test_explicit_empty_systemd_units_not_replaced(tmp_path: Path, isolated_tooling: None) -> None:
    _write_bero_style_minimal(
        tmp_path,
        tooling="""
project:
  name: myapp
paths:
  backend: .
  frontend: f
  scripts: s
  env_local: .env
  email_backend_dir: e
stack:
  healthcheck:
    url: http://localhost/healthz
ops:
  systemd_units:
    pgbackrest: []
    restic: []
""",
    )
    cfg = load_project_config(tmp_path)
    assert cfg.ops.systemd_units.pgbackrest == ()
    assert cfg.ops.systemd_units.restic == ()
