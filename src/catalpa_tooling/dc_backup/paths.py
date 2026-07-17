"""Closed-DC Garage backup (TLS + stack) under ``dk <env> dc-backup``."""

from __future__ import annotations

# Host paths on the DC backup machine (Garage + Caddy).
GARAGE_COMPOSE_DIR = "/opt/garage"
GARAGE_COMPOSE_FILE = f"{GARAGE_COMPOSE_DIR}/docker-compose.yml"
GARAGE_TOML_PATH = "/etc/garage.toml"
GARAGE_CADDYFILE_PATH = "/etc/garage/Caddyfile"
GARAGE_TLS_DIR = "/etc/garage/tls"
GARAGE_META_DIR = "/var/lib/garage/meta"
GARAGE_DATA_DIR = "/var/lib/garage/data"
GARAGE_CADDY_DATA_DIR = "/var/lib/garage/caddy-data"

GARAGE_TLS_CA_NAME = "ca.crt"
GARAGE_TLS_SERVER_CRT_NAME = "server.crt"
GARAGE_TLS_SERVER_KEY_NAME = "server.key"

# App deploy host CA (under ops.config_dir).
APP_CA_FILENAME = "dc-backup-ca.crt"

DC_BACKUP_TLS_FILENAME = "dc-backup-tls.yaml"
DC_BACKUP_FILENAME = "dc-backup.yaml"

DEFAULT_GARAGE_IMAGE = "dxflrs/garage:v2.3.0"
DEFAULT_CADDY_IMAGE = "caddy:2.9-alpine"
DEFAULT_GARAGE_REGION = "garage"

INFO_DC_BACKUP_DOCKER_HOST = "dc_backup_docker_host"
