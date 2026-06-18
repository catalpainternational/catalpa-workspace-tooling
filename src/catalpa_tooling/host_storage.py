"""Orchestrate host storage (path verify, optional DO volumes, Docker bind volumes)."""

from __future__ import annotations

import sys
from typing import Any

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.deploy_do_link import (
    find_droplet_for_link,
    is_digitalocean_host_disabled,
    resolve_env_do_link,
)
from catalpa_tooling.doctl_block_storage import ensure_do_volume, ensure_volume_attached
from catalpa_tooling.host_storage_mount import (
    mount_do_volume_at_path,
    ssh_target_from_docker_host,
    verify_host_mount_path,
    verify_host_mount_path_for_docker_host,
)
from catalpa_tooling.pgbackrest_volume_config import ensure_external_stack_volumes
from catalpa_tooling.storage_config import (
    default_do_volume_name,
    StorageVolumeSpec,
    volume_hosts_from_specs,
)


def _create_host_path_flags(specs: dict[str, StorageVolumeSpec]) -> dict[str, bool]:
    """``True`` for volume keys whose DO block will create/mount the host path."""
    return {key: spec.digitalocean is not None for key, spec in specs.items()}


def _resolve_do_region(
    info: dict[str, Any],
    config: ProjectConfig,
    droplet: dict[str, Any] | None,
) -> str | None:
    do_block = info.get("digitalocean")
    if isinstance(do_block, dict):
        region = str(do_block.get("region") or "").strip()
        if region:
            return region
    if config.digitalocean and config.digitalocean.region:
        return config.digitalocean.region.strip()
    if droplet is not None:
        region = droplet.get("region")
        if isinstance(region, dict):
            slug = region.get("slug")
            if slug:
                return str(slug).strip()
        if region:
            return str(region).strip()
    return None


def ensure_do_block_volumes_for_specs(
    config: ProjectConfig,
    env_name: str,
    info: dict[str, Any],
    specs: dict[str, StorageVolumeSpec],
    *,
    context: str | None = None,
    dry_run: bool = False,
) -> int:
    """Provision and attach DO block volumes; mount at configured paths via SSH."""
    do_specs = {k: v for k, v in specs.items() if v.digitalocean is not None}
    if not do_specs:
        return 0

    if is_digitalocean_host_disabled(info):
        print(
            "storage: digitalocean.disabled is set; skipping DO block volume provisioning. "
            "Use path-only storage entries or set disabled: false.",
            file=sys.stderr,
        )
        return 1

    docker_host = str(info.get("docker_host") or "").strip()
    ssh_target = ssh_target_from_docker_host(docker_host)
    if not ssh_target:
        print(
            "storage: DO volume provisioning requires ssh:// docker_host in info.yaml.",
            file=sys.stderr,
        )
        return 1

    link = resolve_env_do_link(config, env_name, info)
    droplet = find_droplet_for_link(config, link, context=context)
    if droplet is None:
        print(
            f"storage: no DigitalOcean droplet named {link.droplet_name!r} for DO volume attach.",
            file=sys.stderr,
        )
        return 1
    droplet_id = str(droplet.get("id") or "").strip()
    if not droplet_id:
        print("storage: droplet record missing id.", file=sys.stderr)
        return 1

    region = _resolve_do_region(info, config, droplet)
    if not region:
        print(
            "storage: could not resolve region for DO volume (set digitalocean.region in "
            "info.yaml or tooling.yaml).",
            file=sys.stderr,
        )
        return 1

    compose_project = ""
    env_map = info.get("env")
    if isinstance(env_map, dict):
        compose_project = str(env_map.get("compose_project_name") or "").strip()

    if dry_run:
        names = [
            spec.digitalocean.name
            if spec.digitalocean and spec.digitalocean.name
            else default_do_volume_name(
                key,
                droplet_name=link.droplet_name,
                compose_project_name=compose_project,
            )
            for key, spec in do_specs.items()
        ]
        print(
            f"storage (dry-run): would ensure DO volumes {names!r} in {region}, "
            f"attach to droplet {droplet_id}, mount via SSH.",
            file=sys.stderr,
        )
        return 0

    from catalpa_tooling.doctl_binary import ensure_doctl_available

    ensure_doctl_available()

    for key, spec in do_specs.items():
        do_spec = spec.digitalocean
        if do_spec is None:
            continue
        vol_name = do_spec.name or default_do_volume_name(
            key,
            droplet_name=link.droplet_name,
            compose_project_name=compose_project,
        )
        volume = ensure_do_volume(vol_name, do_spec.size_gib, region, context=context)
        rc = ensure_volume_attached(volume, droplet_id, context=context)
        if rc != 0:
            return rc
        rc = mount_do_volume_at_path(
            ssh_target,
            vol_name,
            spec.path,
            filesystem=do_spec.filesystem,
            label=f"storage.{key}",
        )
        if rc != 0:
            return rc
    return 0


def ensure_path_only_storage(
    info: dict[str, Any],
    specs: dict[str, StorageVolumeSpec],
    *,
    dry_run: bool = False,
) -> int:
    """Verify host paths for entries without ``digitalocean`` provisioning."""
    path_only = {k: v for k, v in specs.items() if v.digitalocean is None}
    if not path_only:
        return 0
    docker_host = str(info.get("docker_host") or "").strip()
    if dry_run:
        paths = [spec.path for spec in path_only.values()]
        print(f"storage (dry-run): would verify host paths: {paths!r}", file=sys.stderr)
        return 0
    for key, spec in path_only.items():
        rc = verify_host_mount_path_for_docker_host(
            docker_host,
            spec.path,
            label=f"storage.{key}",
        )
        if rc != 0:
            return rc
    return 0


def ensure_host_storage(
    config: ProjectConfig,
    env_name: str,
    info: dict[str, Any],
    specs: dict[str, StorageVolumeSpec],
    *,
    context: str | None = None,
    dry_run: bool = False,
    ensure_docker_volumes: bool = True,
    env_add: dict[str, str] | None = None,
) -> int:
    """Path verify, optional DO provision/mount, then Docker named-volume bind."""
    if not specs:
        return 0

    rc = ensure_path_only_storage(info, specs, dry_run=dry_run)
    if rc != 0:
        return rc

    rc = ensure_do_block_volumes_for_specs(
        config,
        env_name,
        info,
        specs,
        context=context,
        dry_run=dry_run,
    )
    if rc != 0:
        return rc

    if not ensure_docker_volumes:
        return 0
    if env_add is None:
        print("storage: internal error — env_add required for Docker volume ensure.", file=sys.stderr)
        return 1

    volume_hosts = volume_hosts_from_specs(specs)
    create_host_paths = _create_host_path_flags(specs)
    return ensure_external_stack_volumes(
        env_add,
        dry_run=dry_run,
        config=config,
        volume_hosts=volume_hosts,
        create_host_paths=create_host_paths,
    )


def ensure_host_storage_docker_only(
    config: ProjectConfig,
    env_add: dict[str, str],
    specs: dict[str, StorageVolumeSpec],
    *,
    dry_run: bool = False,
) -> int:
    """Docker bind volumes only (host paths already verified/mounted)."""
    if not specs:
        return 0
    volume_hosts = volume_hosts_from_specs(specs)
    create_host_paths = _create_host_path_flags(specs)
    return ensure_external_stack_volumes(
        env_add,
        dry_run=dry_run,
        config=config,
        volume_hosts=volume_hosts,
        create_host_paths=create_host_paths,
    )
