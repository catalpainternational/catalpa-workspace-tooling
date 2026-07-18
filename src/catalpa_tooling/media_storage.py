"""Resolve where Django media lives for an env: named Docker volume or host bind mount."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.restic_files import (
    _default_compose_project,
    _docker_host_targets_local_engine,
    django_media_volume_name,
)

MEDIA_CONTAINER_PATH = "/media"


class MediaStorageKind(str, Enum):
    VOLUME = "volume"
    BIND = "bind"


@dataclass(frozen=True)
class MediaStorage:
    """Where media files are stored for pull/push/transfer."""

    kind: MediaStorageKind
    # Named Docker volume (kind=volume) or absolute host path (kind=bind).
    location: str

    def describe(self) -> str:
        if self.kind is MediaStorageKind.BIND:
            return f"bind {self.location}"
        return f"volume {self.location}"


class MediaStorageError(ValueError):
    """Cannot resolve or use media storage for this environment."""


def _parse_volume_entry(entry: object) -> tuple[str, str, str] | None:
    """Return ``(type, source, target)`` with type ``bind`` or ``volume``, or None."""
    if isinstance(entry, str):
        # short syntax: source:target[:mode]
        parts = entry.split(":")
        if len(parts) < 2:
            return None
        source, target = parts[0], parts[1]
        # Absolute or relative host path → bind; bare name → named volume.
        if source.startswith("/") or source.startswith("./") or source.startswith("../"):
            return "bind", source, target
        if source.startswith("~"):
            return "bind", source, target
        # Windows-style or bare relative without ./ is uncommon; treat non-volume-looking as bind.
        if "/" in source or "\\" in source:
            return "bind", source, target
        return "volume", source, target

    if isinstance(entry, dict):
        target = str(entry.get("target") or entry.get("destination") or "").strip()
        if not target:
            return None
        typ = str(entry.get("type") or "").strip().lower()
        source = str(entry.get("source") or entry.get("bind") or "").strip()
        if typ == "bind" or (not typ and source and (source.startswith("/") or source.startswith("."))):
            if not source:
                return None
            return "bind", source, target
        if typ == "volume" or (not typ and source):
            if not source:
                # anonymous volume
                return None
            return "volume", source, target
        if typ == "tmpfs":
            return None
    return None


def _iter_service_volume_entries(services: dict[str, Any]) -> list[tuple[str, object]]:
    """``(service_name, volume_entry)`` for every volumes list item."""
    out: list[tuple[str, object]] = []
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        vols = svc.get("volumes")
        if not isinstance(vols, list):
            continue
        for entry in vols:
            out.append((str(name), entry))
    return out


def _preferred_service_order(config: ProjectConfig | None) -> list[str]:
    if config is None:
        return ["django", "web"]
    web = (config.stack.services.web or "").strip()
    order = [web] if web else []
    for name in ("django", "web"):
        if name and name not in order:
            order.append(name)
    return order


def find_media_mount_in_compose(
    compose_path: Path,
    *,
    config: ProjectConfig | None = None,
) -> tuple[str, str, str] | None:
    """Parse compose YAML; return ``(kind, source, target)`` for a ``/media`` mount, or None.

    Prefers the stack web service, then any other service with target ``/media``.
    Ignores ``/srv/media`` (Caddy) mounts.
    """
    if not compose_path.is_file():
        return None
    try:
        data = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    services = data.get("services")
    if not isinstance(services, dict):
        return None

    preferred = _preferred_service_order(config)
    found: dict[str, tuple[str, str, str]] = {}

    for svc_name, entry in _iter_service_volume_entries(services):
        parsed = _parse_volume_entry(entry)
        if parsed is None:
            continue
        kind, source, target = parsed
        if target.rstrip("/") != MEDIA_CONTAINER_PATH:
            continue
        found[svc_name] = (kind, source, target)

    for name in preferred:
        if name in found:
            return found[name]
    if found:
        # Deterministic: first service name sorted.
        key = sorted(found.keys())[0]
        return found[key]
    return None


def resolve_bind_host_path(source: str, *, compose_path: Path, repo_root: Path) -> Path:
    """Resolve a compose bind ``source`` to an absolute host path."""
    raw = source.strip()
    if raw.startswith("~"):
        return Path(raw).expanduser().resolve()
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    # Relative binds are relative to the compose file's directory (Compose spec).
    base = compose_path.parent.resolve()
    return (base / p).resolve()


def resolve_media_storage(
    *,
    compose_file: str,
    env: dict[str, str],
    config: ProjectConfig,
    repo_root: Path | None = None,
) -> MediaStorage:
    """Resolve media storage for an environment from its compose file.

    Named-volume mounts (or no ``/media`` mount) → ``{project}_django_media``.
    Host bind mounts → absolute path. Remote ``DOCKER_HOST`` + bind raises
    ``MediaStorageError``.
    """
    root = (repo_root or config.repo_root).resolve()
    compose_path = Path(compose_file)
    if not compose_path.is_absolute():
        compose_path = (root / compose_path).resolve()

    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip() or _default_compose_project(config)
    default_vol = django_media_volume_name(project, config=config)

    mount = find_media_mount_in_compose(compose_path, config=config)
    if mount is None:
        return MediaStorage(MediaStorageKind.VOLUME, default_vol)

    kind, source, _target = mount
    if kind == "volume":
        # Compose volume key (e.g. django_media) → Docker volume name with project prefix.
        # Explicit ``name:`` overrides are rare; transfer historically used ops.restic key.
        vol_key = source.strip()
        expected_key = (
            (config.ops.restic.data_volume if config else None) or "django_media"
        ).strip()
        if vol_key == expected_key or not vol_key:
            return MediaStorage(MediaStorageKind.VOLUME, default_vol)
        # Non-standard volume key still names as {project}_{key}.
        return MediaStorage(MediaStorageKind.VOLUME, f"{project}_{vol_key}")

    # bind
    host_path = resolve_bind_host_path(source, compose_path=compose_path, repo_root=root)
    docker_host = str(env.get("DOCKER_HOST") or "").strip()
    if not _docker_host_targets_local_engine(docker_host):
        raise MediaStorageError(
            f"compose file {compose_path} mounts media as a host bind ({host_path}), "
            f"but DOCKER_HOST={docker_host!r} is remote. Bind media transfer only works "
            "with the local Docker engine."
        )
    return MediaStorage(MediaStorageKind.BIND, str(host_path))


def media_storage_label(storage: MediaStorage) -> str:
    """Short label for logs / confirmation."""
    return storage.describe()
