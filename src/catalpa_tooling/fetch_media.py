"""Rsync media from a deploy host (Docker volume or legacy host path)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.media_rsync import (
    docker_volume_mountpoint_ssh,
    rsync_between_endpoints,
    ssh_target_from_host,
)
from catalpa_tooling.restic_files import django_media_volume_name
from catalpa_tooling.ssh_known_hosts import ensure_ssh_known_host_for_ssh_target
from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target


def _ensure_ssh_target_known(ssh_target: str) -> None:
    if ensure_ssh_known_host_for_ssh_target(ssh_target) != 0:
        raise SystemExit(1)


def dk_info_fetch_media_defaults(config: ProjectConfig, env_name: str) -> tuple[str, str]:
    """Return ``(ssh_user@host, compose_project_name)`` from ``docker/envs/<env>/info.yaml``."""
    info_path = config.deploy_envs_dir / env_name / "info.yaml"
    if not info_path.is_file():
        raise FileNotFoundError(f"Missing {info_path}")
    with open(info_path, encoding="utf-8") as f:
        info = yaml.safe_load(f) or {}
    try:
        ssh = parse_docker_host_to_ssh_target(str(info.get("docker_host", "") or ""))
    except ValueError as exc:
        raise ValueError(f"{info_path}: {exc}") from exc
    env_block = info.get("env") or {}
    if not isinstance(env_block, dict):
        env_block = {}
    default_project = config.stack.compose_project_default
    project = str(env_block.get("compose_project_name") or default_project).strip()
    project = project or default_project
    return ssh, project


PARTIAL_SUBDIRS = ("documents", "original_images")


def pull_media_target(config: ProjectConfig, env_name: str, target: str | None) -> Path:
    """Warn that ``dk <env> pull_media`` is deprecated; return where it copies to: ``--target``
    (relative to the repo root, as before), else ``fetch media``'s ``native.fetch_media.dest``."""
    from catalpa_tooling.deprecation import warn_deprecated

    where = f"--target {target}" if target else f"native.fetch_media.dest ({config.native.fetch_media.dest})"
    warn_deprecated(
        f"dk {env_name} pull_media",
        f"dk fetch media --env {env_name}",
        context=f"it does the same; copying into {where}",
    )
    raw = Path(target).expanduser() if target else config.fetch_media_dest_path
    return (raw if raw.is_absolute() else config.repo_root / raw).resolve()


def _rsync_into(source: str, local: Path) -> None:
    """rsync ``source/`` into ``local`` (adds and updates, deletes nothing)."""
    local.mkdir(parents=True, exist_ok=True)
    rc = rsync_between_endpoints(source, str(local), delete=False, dry_run=False)
    if rc != 0:
        raise SystemExit(rc)


def _fetch_from_backend(config: ProjectConfig, dk_env: str, local_base: Path, *, partial: bool) -> None:
    """The env's stack, whichever runtime runs it, finds its media (``StackBackend``)."""
    from catalpa_tooling import stack_backend
    from catalpa_tooling.fcos.backend import Usage
    from catalpa_tooling.media_storage import MediaStorageError

    try:
        b = stack_backend.backend_for(config, dk_env)
        print(f"Fetching media from {b.describe()} → {local_base}", file=sys.stderr)
        if not partial:
            rc = b.pull_media(local_base)
            if rc != 0:
                raise SystemExit(rc)
            return
        source = b.media_rsync_source()
    except (stack_backend.BackendError, MediaStorageError, Usage) as exc:
        raise ValueError(str(exc)) from None
    if source is None:
        raise ValueError(f"--partial needs rsync, and {dk_env}'s media can only be copied with tar")
    for sub in PARTIAL_SUBDIRS:
        _rsync_into(f"{source}/{sub}", local_base / sub)


def run_fetch_media(
    config: ProjectConfig,
    *,
    dk_env: str,
    host: str | None,
    dest: Path | None,
    partial: bool,
    legacy_path: bool,
    legacy_remote: str | None,
    compose_project: str | None,
) -> None:
    """Sync media into ``dest`` (default: ``native.fetch_media.dest`` under repo root).

    By default the env's backend says where the media is (``stack_backend``). ``--legacy-path``
    (a directory on a host), ``--host`` and ``--compose-project`` (a named Docker volume on a
    given host) keep their own paths.
    """
    if not shutil.which("rsync"):
        print("rsync is not installed or not on PATH.", file=sys.stderr)
        raise SystemExit(1)
    if not shutil.which("ssh"):
        print("ssh is not installed or not on PATH.", file=sys.stderr)
        raise SystemExit(1)

    local_base = (dest if dest is not None else config.fetch_media_dest_path).resolve()

    if legacy_path:
        legacy = config.native.fetch_media.legacy
        remote_base = (legacy_remote or (legacy.remote if legacy else "")).rstrip("/")
        if not remote_base:
            raise ValueError(
                "legacy path mode requires native.fetch_media.legacy.remote in tooling.yaml "
                "or --remote PATH"
            )
        ssh_host = host
        if ssh_host is None and legacy and legacy.ssh_host:
            ssh_host = legacy.ssh_host
        if not ssh_host:
            raise ValueError(
                "legacy path mode requires --host USER@HOST or native.fetch_media.legacy.ssh_host "
                "in tooling.yaml"
            )
        ssh_target = ssh_target_from_host(ssh_host)
        print(f"Rsync legacy path {remote_base!r} from {ssh_target} …", file=sys.stderr)
    elif host is None and compose_project is None:
        _fetch_from_backend(config, dk_env, local_base, partial=partial)
        print(f"Done: {local_base}", file=sys.stderr)
        return
    else:  # a compose volume on an explicit host and/or project
        default_ssh = default_project = ""
        if host is None or compose_project is None:
            default_ssh, default_project = dk_info_fetch_media_defaults(config, dk_env)
        ssh_target = ssh_target_from_host(host) if host else default_ssh
        project = compose_project if compose_project is not None else default_project
        volume = django_media_volume_name(project, config=config)
        print(f"Resolving Docker volume {volume!r} on {ssh_target} …", file=sys.stderr)

    _ensure_ssh_target_known(ssh_target)

    if not legacy_path:
        remote_base = docker_volume_mountpoint_ssh(ssh_target, volume, label="fetch media")

    subdirs = PARTIAL_SUBDIRS if partial else ("",)
    for sub in subdirs:
        _rsync_into(f"{ssh_target}:{remote_base}/{sub}".rstrip("/"), local_base / sub)

    print(f"Done: {local_base}", file=sys.stderr)
