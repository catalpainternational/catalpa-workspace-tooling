"""``digitalocean.spaces`` parsing and defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalpa_tooling.config import ProjectConfig, ProjectConfigError, load_project_config
from catalpa_tooling.doctl_spaces_provision import spaces_backup_defaults


def test_spaces_backup_defaults_without_digitalocean_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    (repo / "tooling.yaml").write_text(
        """
project:
  name: myapp
  root_marker: pyproject.toml
paths:
  backend: b
  frontend: f
  scripts: s
  env_local: .env
  email_backend_dir: e
  fetch_db_dump: d
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
stack:
  compose_project_default: app
  services: {web: w, proxy: p, db: db}
  images:
    registry_key: reg
    components: {web: w, proxy: p, db: db}
  healthcheck: {service: w, url: http://localhost/}
ops:
  install_prefix: /opt/a
  config_dir: /etc/a
  systemd_unit_prefix: a-
  transfer_workdir: .t
  default_db_container: db1
  pgbackrest:
    postgres_conf: a.conf
    pgbackrest_conf: b.conf
    default_registry: ghcr.io/x
    restore_temp_prefix: p_
  zabbix:
    unit_name: z.service
    userparams_file: z.conf
  systemd_units:
    pgbackrest:
      - a-pgbackrest-backup-full.service
      - a-pgbackrest-backup-incr.service
      - a-pgbackrest-backup-diff.service
      - a-pgbackrest-backup-full.timer
    restic:
      - a-restic-files-backup.service
      - a-restic-files-backup.timer
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    cfg = load_project_config(repo)
    d = spaces_backup_defaults(cfg, "prod")
    assert d.bucket == "myapp"
    assert d.region == "sgp1"
    assert d.endpoint == "sgp1.digitaloceanspaces.com"
    assert d.pgbackrest_repo_path == "/myapp/prod/pgbackrest"
    assert d.restic_path == "myapp-prod-media"
    assert d.write_key_name == "myapp-write"


def test_spaces_backup_defaults_with_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    (repo / "tooling.yaml").write_text(
        """
project:
  name: myapp
  root_marker: pyproject.toml
paths:
  backend: b
  frontend: f
  scripts: s
  env_local: .env
  email_backend_dir: e
  fetch_db_dump: d
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
stack:
  compose_project_default: app
  services: {web: w, proxy: p, db: db}
  images:
    registry_key: reg
    components: {web: w, proxy: p, db: db}
  healthcheck: {service: w, url: http://localhost/}
ops:
  install_prefix: /opt/a
  config_dir: /etc/a
  systemd_unit_prefix: a-
  transfer_workdir: .t
  default_db_container: db1
  pgbackrest:
    postgres_conf: a.conf
    pgbackrest_conf: b.conf
    default_registry: ghcr.io/x
    restore_temp_prefix: p_
  zabbix:
    unit_name: z.service
    userparams_file: z.conf
  systemd_units:
    pgbackrest:
      - a-pgbackrest-backup-full.service
      - a-pgbackrest-backup-incr.service
      - a-pgbackrest-backup-diff.service
      - a-pgbackrest-backup-full.timer
    restic:
      - a-restic-files-backup.service
      - a-restic-files-backup.timer
digitalocean:
  region: nyc3
  spaces:
    bucket: backups
    endpoint: nyc3.digitaloceanspaces.com
    pgbackrest_repo_path: /custom/pg
    restic_path: custom-media
    stanza: primary
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    cfg = load_project_config(repo)
    d = spaces_backup_defaults(cfg, "staging")
    assert d.bucket == "backups"
    assert d.region == "nyc3"
    assert d.endpoint == "nyc3.digitaloceanspaces.com"
    assert d.pgbackrest_repo_path == "/custom/pg"
    assert d.restic_path == "custom-media"
    assert d.stanza == "primary"
    assert d.write_key_name == "backups-write"


def test_digitalocean_spaces_must_be_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    (repo / "tooling.yaml").write_text(
        "project: {name: x, root_marker: pyproject.toml}\n"
        "paths: {backend: b, scripts: s, env_local: e, email_backend_dir: e, fetch_db_dump: d,"
        " deploy: {envs_dir: docker/envs, images_config: i.yaml, default_compose: c.yml, dev_compose: d.yml}}\n"
        "stack: {compose_project_default: a, services: {web: w, proxy: p, db: db},"
        " images: {registry_key: r, components: {web: w, proxy: p, db: db}},"
        " healthcheck: {service: w, url: http://x/}}\n"
        "ops: {install_prefix: /o, config_dir: /c, systemd_unit_prefix: a-, transfer_workdir: .t,"
        " default_db_container: d, pgbackrest: {postgres_conf: a, pgbackrest_conf: b,"
        " default_registry: x, restore_temp_prefix: p}, zabbix: {unit_name: z, userparams_file: u},"
        " systemd_units: {pgbackrest: [s], restic: [t]}}\n"
        "digitalocean: {spaces: not-a-map}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    with pytest.raises(ProjectConfigError, match="digitalocean.spaces"):
        load_project_config(repo)
