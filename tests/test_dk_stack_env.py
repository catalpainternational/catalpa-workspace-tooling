"""Tests for catalpa_tooling.dk_stack env loading."""

from pathlib import Path

from catalpa_tooling.config import DEFAULT_BUILD_PLACEHOLDERS, load_project_config
from catalpa_tooling.dk_stack import _apply_build_placeholders, _load_dotenv_file, env_for_stack_build


def test_load_dotenv_file_parses_comments_and_values(tmp_path: Path) -> None:
    env_path = tmp_path / "jid.env"
    env_path.write_text(
        "# comment\nPROJECT_NAME=jid\nDJANGO_DB=jid_db\n",
        encoding="utf-8",
    )
    assert _load_dotenv_file(env_path) == {
        "PROJECT_NAME": "jid",
        "DJANGO_DB": "jid_db",
    }


def test_apply_build_placeholders_only_fills_missing() -> None:
    env = {"POSTGRES_PASSWORD": "real", "BERO_ORIGIN": ""}
    _apply_build_placeholders(env, DEFAULT_BUILD_PLACEHOLDERS)
    assert env["POSTGRES_PASSWORD"] == "real"
    assert env["DJANGO_DB_PASSWORD"] == "build_placeholder"
    assert env["BERO_ORIGIN"] == "https://build.example"


def test_env_for_stack_build_loads_jid_env(tmp_path: Path, isolated_tooling: None) -> None:
    (tmp_path / "pyproject.toml").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(
        """
project:
  name: jid
  root_marker: pyproject.toml
paths:
  backend: .
  frontend: bero
  scripts: scripts
  env_local: .env.local
  email_backend_dir: media
  fetch_db_dump: dump.custom
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yaml
    dev_compose: compose.dev.yaml
    credentials_optional_envs: [dev]
native:
  reset_db:
    postgis: false
    db_name_env: [DJANGO_DB]
    host_env: [POSTGRES_HOST]
    port_env: [POSTGRES_PORT]
    user_env: [POSTGRES_USER]
    password_env: [POSTGRES_PASSWORD]
stack:
  compose_project_default: jid
  services:
    web: django
    proxy: caddy
    db: db
  images:
    registry_key: image_registry
    components:
      web: jid-django
      proxy: jid-caddy
      db: jid-db
  healthcheck:
    service: django
    url: http://localhost:8000/cms/
ops:
  install_prefix: /opt/jid
  config_dir: /etc/jid
  systemd_unit_prefix: jid-
  transfer_workdir: .jid-transfer
  default_db_container: jid-db-1
  pgbackrest:
    postgres_conf: 50.conf
    pgbackrest_conf: 50.conf
    default_registry: ghcr.io/example
    restore_temp_prefix: jid_
    data_volume: postgres_data
    pg1_path: /var/lib/postgresql/data
  restic:
    data_volume: django_media
  zabbix:
    unit_name: jid-zabbix.service
    userparams_file: 99.conf
  systemd_units:
    pgbackrest: []
    restic: []
    timers_enable_pgbackrest: []
    timers_enable_restic: []
digitalocean:
  project_name: JID
  timezone: UTC
  region: sgp1
  size: s-2vcpu-4gb
""",
        encoding="utf-8",
    )
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "images.yaml").write_text("image_registry: ghcr.io/example\n", encoding="utf-8")
    (tmp_path / "jid.env").write_text("PROJECT_NAME=jid\nDJANGO_DB=jid_db\n", encoding="utf-8")
    config = load_project_config(tmp_path)
    env = env_for_stack_build(config)
    assert env["PROJECT_NAME"] == "jid"
    assert env["DJANGO_DB"] == "jid_db"
    assert env["POSTGRES_PASSWORD"] == "build_placeholder"
    assert "STACK_IMAGE_TAG" in env
