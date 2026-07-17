"""Closed-DC Garage backup: TLS + stack under ``dk <env> dc-backup``."""

from __future__ import annotations

from catalpa_tooling.dc_backup.cli import handle_dc_backup_command
from catalpa_tooling.dc_backup.hosts import (
    DC_BACKUP_CA_CONTAINER_PATH,
    DC_BACKUP_CA_FILE_ENV,
    DOCKER_ADD_HOST_ENV,
    apply_inferred_dc_backup_ca_file,
    compose_dash_f_args,
    dc_backup_ca_host_path,
    dc_backup_tls_extra_compose_files,
    docker_add_host_args,
    docker_ca_env_flags_for_restic,
    docker_ca_volume_args,
    merge_extra_compose_files,
    parse_docker_add_hosts,
    write_dc_backup_tls_override,
)

__all__ = [
    "DC_BACKUP_CA_CONTAINER_PATH",
    "DC_BACKUP_CA_FILE_ENV",
    "DOCKER_ADD_HOST_ENV",
    "apply_inferred_dc_backup_ca_file",
    "compose_dash_f_args",
    "dc_backup_ca_host_path",
    "dc_backup_tls_extra_compose_files",
    "docker_add_host_args",
    "docker_ca_env_flags_for_restic",
    "docker_ca_volume_args",
    "handle_dc_backup_command",
    "merge_extra_compose_files",
    "parse_docker_add_hosts",
    "write_dc_backup_tls_override",
]
