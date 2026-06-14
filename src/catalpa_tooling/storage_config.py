"""Parse ``storage`` block from ``docker/envs/<env>/info.yaml`` for host-bound Docker volumes."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from catalpa_tooling.config import ProjectConfig

DEFAULT_DO_VOLUME_FILESYSTEM = "ext4"
CADDY_DATA_VOLUME_KEY = "caddy_data"
_DO_VOLUME_NAME_MAX_LEN = 64


class StorageConfigError(ValueError):
    """Invalid ``storage`` section in info.yaml."""


def sanitize_do_volume_name(name: str) -> str:
    """Normalize a block volume name for the DO API (lowercase ``a-z``, digits, ``-`` only)."""
    s = name.strip().lower().replace("_", "-")
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        raise StorageConfigError("DO volume name is empty after sanitization")
    if not s[0].isalpha():
        s = f"v-{s}"
    if len(s) > _DO_VOLUME_NAME_MAX_LEN:
        s = s[:_DO_VOLUME_NAME_MAX_LEN].rstrip("-")
    return s


@dataclass(frozen=True)
class DigitalOceanVolumeSpec:
    """Optional DO block volume provisioning for a compose data volume."""

    name: str | None
    size_gib: int
    filesystem: str


@dataclass(frozen=True)
class StorageVolumeSpec:
    """Host path (+ optional DO provisioning) for one compose volume key."""

    key: str
    path: str
    digitalocean: DigitalOceanVolumeSpec | None


def bindable_volume_keys(config: ProjectConfig) -> frozenset[str]:
    """Compose volume keys that may appear under ``storage.volumes``."""
    return frozenset(
        {
            config.ops.restic.data_volume,
            config.ops.pgbackrest.data_volume,
            CADDY_DATA_VOLUME_KEY,
        }
    )


def _validate_abs_path(path: str, *, field: str) -> str:
    s = path.strip()
    if not s.startswith("/"):
        raise StorageConfigError(f"{field} must be an absolute path (got {path!r})")
    if ".." in Path(s).parts:
        raise StorageConfigError(f"{field} must not contain .. (got {path!r})")
    return s.rstrip("/") or "/"


def _parse_do_block(raw: Any, *, field_prefix: str) -> DigitalOceanVolumeSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise StorageConfigError(f"{field_prefix}.digitalocean must be a mapping")
    name_raw = raw.get("name")
    name = str(name_raw).strip() if name_raw is not None and name_raw is not False else None
    if name is not None and not name:
        name = None
    if name is not None:
        name = sanitize_do_volume_name(name)
    size_raw = raw.get("size_gib")
    if size_raw is None:
        raise StorageConfigError(
            f"{field_prefix}.digitalocean.size_gib is required when digitalocean block is set"
        )
    try:
        size_gib = int(size_raw)
    except (TypeError, ValueError) as exc:
        raise StorageConfigError(
            f"{field_prefix}.digitalocean.size_gib must be an integer (got {size_raw!r})"
        ) from exc
    if size_gib <= 0:
        raise StorageConfigError(
            f"{field_prefix}.digitalocean.size_gib must be positive (got {size_gib})"
        )
    fs_raw = raw.get("filesystem")
    filesystem = (
        str(fs_raw).strip() if fs_raw is not None and fs_raw is not False else DEFAULT_DO_VOLUME_FILESYSTEM
    )
    if not filesystem:
        filesystem = DEFAULT_DO_VOLUME_FILESYSTEM
    return DigitalOceanVolumeSpec(name=name, size_gib=size_gib, filesystem=filesystem)


def parse_storage_volumes_from_info(
    info: dict | None,
    config: ProjectConfig,
    *,
    field_prefix: str = "info.yaml storage",
) -> dict[str, StorageVolumeSpec]:
    """Return compose volume key → spec; empty when ``storage`` is absent or has no volumes."""
    if not isinstance(info, dict):
        return {}
    storage = info.get("storage")
    if storage is None:
        return {}
    if not isinstance(storage, dict):
        raise StorageConfigError(f"{field_prefix} must be a mapping")
    volumes_raw = storage.get("volumes")
    if volumes_raw is None:
        return {}
    if not isinstance(volumes_raw, dict):
        raise StorageConfigError(f"{field_prefix}.volumes must be a mapping")
    if not volumes_raw:
        return {}

    allowed = bindable_volume_keys(config)
    out: dict[str, StorageVolumeSpec] = {}
    for key, vol_raw in volumes_raw.items():
        vol_key = str(key).strip()
        if not vol_key:
            raise StorageConfigError(f"{field_prefix}.volumes: empty volume key")
        if vol_key not in allowed:
            raise StorageConfigError(
                f"{field_prefix}.volumes.{vol_key}: unsupported key "
                f"(allowed: {', '.join(sorted(allowed))})"
            )
        entry_field = f"{field_prefix}.volumes.{vol_key}"
        if not isinstance(vol_raw, dict):
            raise StorageConfigError(f"{entry_field} must be a mapping")
        path_raw = vol_raw.get("path")
        if path_raw is None or path_raw is False or not str(path_raw).strip():
            raise StorageConfigError(f"{entry_field}.path is required")
        path = _validate_abs_path(str(path_raw), field=f"{entry_field}.path")
        do_block = _parse_do_block(vol_raw.get("digitalocean"), field_prefix=entry_field)
        out[vol_key] = StorageVolumeSpec(key=vol_key, path=path, digitalocean=do_block)
    return out


def volume_hosts_from_specs(specs: dict[str, StorageVolumeSpec]) -> dict[str, str]:
    """Map compose volume key → host mount path for Docker bind volume creation."""
    return {key: spec.path for key, spec in specs.items()}


def default_do_volume_name(
    volume_key: str,
    *,
    droplet_name: str | None = None,
    compose_project_name: str | None = None,
) -> str:
    """Default DO block volume name when ``digitalocean.name`` is omitted."""
    prefix = (droplet_name or "").strip() or (compose_project_name or "").strip()
    raw = volume_key if not prefix else f"{prefix}-{volume_key}"
    return sanitize_do_volume_name(raw)


def parse_storage_volumes_or_exit(
    info: dict | None,
    config: ProjectConfig,
) -> dict[str, StorageVolumeSpec]:
    """Like ``parse_storage_volumes_from_info`` but print and exit on invalid config."""
    try:
        return parse_storage_volumes_from_info(info, config)
    except StorageConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


def volume_bind_kwargs(config: ProjectConfig, env_name: str) -> dict[str, dict[str, str] | dict[str, bool]]:
    """``volume_hosts`` / ``create_host_paths`` kwargs for ensure volume helpers."""
    info_path = config.deploy_envs_dir / env_name / "info.yaml"
    if not info_path.is_file():
        return {}
    import yaml

    with open(info_path, encoding="utf-8") as f:
        info = yaml.safe_load(f) or {}
    try:
        specs = parse_storage_volumes_from_info(info, config)
    except StorageConfigError:
        return {}
    if not specs:
        return {}
    return {
        "volume_hosts": volume_hosts_from_specs(specs),
        "create_host_paths": {k: spec.digitalocean is not None for k, spec in specs.items()},
    }


def load_info_storage_volumes(
    config: ProjectConfig,
    env_name: str,
) -> dict[str, StorageVolumeSpec] | None:
    """Load and parse ``storage`` from ``docker/envs/<env>/info.yaml``; None if missing file."""
    info_path = config.deploy_envs_dir / env_name / "info.yaml"
    if not info_path.is_file():
        return None
    import yaml

    with open(info_path, encoding="utf-8") as f:
        info = yaml.safe_load(f) or {}
    try:
        return parse_storage_volumes_from_info(info, config)
    except StorageConfigError as exc:
        print(f"{info_path}: {exc}", file=sys.stderr)
        return None
