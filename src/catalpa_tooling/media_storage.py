"""Resolve where Django media lives for an env: named Docker volume or host bind mount."""

from __future__ import annotations

import json
import os
import subprocess
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
from catalpa_tooling.run_cmd import run as run_cmd

# Legacy / common container targets when DJANGO_MEDIA_ROOT is unset.
MEDIA_CONTAINER_PATH = "/media"
_DEFAULT_MEDIA_TARGETS: frozenset[str] = frozenset({"/media", "/django_media"})
_IGNORED_MEDIA_TARGETS: frozenset[str] = frozenset({"/srv/media"})


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


def _data_volume_key(config: ProjectConfig | None) -> str:
    return ((config.ops.restic.data_volume if config else None) or "django_media").strip()


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


def _service_media_targets(svc: dict[str, Any]) -> frozenset[str]:
    """Container paths that count as Django media for this service."""
    targets = set(_DEFAULT_MEDIA_TARGETS)
    env = svc.get("environment")
    if isinstance(env, dict):
        root = str(env.get("DJANGO_MEDIA_ROOT") or "").strip()
        if root:
            targets.add(root.rstrip("/") or root)
    elif isinstance(env, list):
        for item in env:
            if isinstance(item, str) and item.startswith("DJANGO_MEDIA_ROOT="):
                root = item.split("=", 1)[1].strip()
                if root:
                    targets.add(root.rstrip("/") or root)
    return frozenset(targets)


def _normalize_target(target: str) -> str:
    t = target.strip()
    if len(t) > 1:
        t = t.rstrip("/")
    return t


def _is_media_mount(
    *,
    kind: str,
    source: str,
    target: str,
    media_targets: frozenset[str],
    data_volume_key: str,
) -> bool:
    """True when this mount is the Django media tree (not Caddy ``/srv/media``)."""
    norm = _normalize_target(target)
    if norm in _IGNORED_MEDIA_TARGETS or target.strip() in _IGNORED_MEDIA_TARGETS:
        return False
    if kind == "volume" and source.strip() == data_volume_key:
        return True
    return norm in media_targets or target.strip() in media_targets


def _pick_mount_from_found(
    found: dict[str, tuple[str, str, str]],
    preferred: list[str],
) -> tuple[str, str, str] | None:
    for name in preferred:
        if name in found:
            return found[name]
    if found:
        key = sorted(found.keys())[0]
        return found[key]
    return None


def find_media_mount_in_config(
    data: dict[str, Any],
    *,
    config: ProjectConfig | None = None,
) -> tuple[str, str, str] | None:
    """Find media mount in merged Compose config JSON.

    Returns ``(kind, source, target)`` where ``kind`` is ``bind`` or ``volume``.
    Prefers the stack web service. Matches ``ops.restic.data_volume`` volume keys,
    ``DJANGO_MEDIA_ROOT``, and common targets ``/media`` / ``/django_media``.
    """
    services = data.get("services")
    if not isinstance(services, dict):
        return None

    preferred = _preferred_service_order(config)
    data_vol = _data_volume_key(config)
    found: dict[str, tuple[str, str, str]] = {}

    for svc_name, entry in _iter_service_volume_entries(services):
        parsed = _parse_volume_entry(entry)
        if parsed is None:
            continue
        kind, source, target = parsed
        svc = services.get(svc_name) if isinstance(services.get(svc_name), dict) else {}
        assert isinstance(svc, dict)
        media_targets = _service_media_targets(svc)
        if not _is_media_mount(
            kind=kind,
            source=source,
            target=target,
            media_targets=media_targets,
            data_volume_key=data_vol,
        ):
            continue
        found[svc_name] = (kind, source, target)

    return _pick_mount_from_found(found, preferred)


def find_media_mount_in_compose(
    compose_path: Path,
    *,
    config: ProjectConfig | None = None,
) -> tuple[str, str, str] | None:
    """Parse a single compose YAML file (no ``include`` merge); return media mount or None.

    Prefer :func:`load_compose_config` + :func:`find_media_mount_in_config` when Docker
    is available so includes and ``${VAR}`` interpolation are resolved.
    """
    if not compose_path.is_file():
        return None
    try:
        data = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    return find_media_mount_in_config(data, config=config)


def _merged_process_env(env: dict[str, str]) -> dict[str, str]:
    out = os.environ.copy()
    for k, v in env.items():
        if v is not None:
            out[k] = str(v)
    return out


def load_compose_config(
    compose_file: str,
    env: dict[str, str],
    *,
    cwd: Path | None = None,
) -> dict[str, Any] | None:
    """Run ``docker compose config --format json``; return parsed mapping or None."""
    r = run_cmd(
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "config",
            "--format",
            "json",
        ],
        env=_merged_process_env(env),
        cwd=str(cwd) if cwd is not None else None,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def resolve_bind_host_path(source: str, *, compose_path: Path, repo_root: Path) -> Path:
    """Resolve a compose bind ``source`` to an absolute host path."""
    del repo_root  # reserved; absolute sources need no repo-relative base
    raw = source.strip()
    if raw.startswith("~"):
        return Path(raw).expanduser().resolve()
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    # Relative binds are relative to the compose file's directory (Compose spec).
    base = compose_path.parent.resolve()
    return (base / p).resolve()


def _docker_volume_name_for_key(
    vol_key: str,
    *,
    project: str,
    config: ProjectConfig,
    compose_volumes: dict[str, Any] | None,
) -> str:
    """Map compose volume key to Docker volume name (honors top-level ``name:``)."""
    expected_key = _data_volume_key(config)
    if compose_volumes and isinstance(compose_volumes.get(vol_key), dict):
        explicit = str(compose_volumes[vol_key].get("name") or "").strip()
        if explicit:
            return explicit
    if vol_key == expected_key or not vol_key:
        return django_media_volume_name(project, config=config)
    return f"{project}_{vol_key}"


def resolve_media_storage(
    *,
    compose_file: str,
    env: dict[str, str],
    config: ProjectConfig,
    repo_root: Path | None = None,
) -> MediaStorage:
    """Resolve media storage for an environment from its compose file.

    Prefers merged ``docker compose config`` (includes + env interpolation). Falls back
    to parsing the compose YAML when config fails. Named-volume mounts (or no media
    mount) → ``{project}_django_media`` (or compose ``name:``). Host binds → absolute
    path. Remote ``DOCKER_HOST`` + bind raises ``MediaStorageError``.
    """
    root = (repo_root or config.repo_root).resolve()
    compose_path = Path(compose_file)
    if not compose_path.is_absolute():
        compose_path = (root / compose_path).resolve()

    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip() or _default_compose_project(config)
    default_vol = django_media_volume_name(project, config=config)

    compose_data = load_compose_config(str(compose_path), env, cwd=root)
    compose_volumes: dict[str, Any] | None = None
    if compose_data is not None:
        vols = compose_data.get("volumes")
        compose_volumes = vols if isinstance(vols, dict) else None
        mount = find_media_mount_in_config(compose_data, config=config)
    else:
        mount = find_media_mount_in_compose(compose_path, config=config)

    if mount is None:
        return MediaStorage(MediaStorageKind.VOLUME, default_vol)

    kind, source, _target = mount
    if kind == "volume":
        vol_key = source.strip()
        return MediaStorage(
            MediaStorageKind.VOLUME,
            _docker_volume_name_for_key(
                vol_key,
                project=project,
                config=config,
                compose_volumes=compose_volumes,
            ),
        )

    # bind — compose config already expands to absolute paths when available
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
