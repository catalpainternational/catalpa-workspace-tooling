"""Build env for ``scripts/fetch_media.sh`` (docker volume vs legacy host path)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

DEFAULT_MEDIA_DK_ENV = "staging"
LEGACY_REMOTE_MEDIA_PATH = "/backup/django_media"


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


def build_fetch_media_env(
    config: ProjectConfig,
    *,
    legacy_path: bool,
    dk_env: str,
    host: str | None,
    remote_path: str,
    dest: Path | None,
    partial: bool,
    compose_project: str | None,
) -> dict[str, str]:
    """Process env for ``fetch_media.sh`` (merge over ``os.environ``)."""
    env = os.environ.copy()
    repo_root = config.repo_root
    local_base = (dest if dest is not None else repo_root / "media").resolve()
    env["FETCH_MEDIA_DEST"] = str(local_base)
    env["FETCH_MEDIA_FULL"] = "0" if partial else "1"
    if legacy_path:
        env["FETCH_MEDIA_SOURCE"] = "path"
        env["FETCH_MEDIA_SSH_HOST"] = host or ""
        if not env["FETCH_MEDIA_SSH_HOST"]:
            raise ValueError(
                "legacy path mode requires --host (SSH target) or set FETCH_MEDIA_SSH_HOST"
            )
        env["FETCH_MEDIA_REMOTE"] = remote_path.rstrip("/")
        env.pop("FETCH_COMPOSE_PROJECT_NAME", None)
        env.pop("FETCH_MEDIA_DOCKER_VOLUME", None)
    else:
        env["FETCH_MEDIA_SOURCE"] = "docker_volume"
        env.pop("FETCH_MEDIA_REMOTE", None)
        if host is None:
            ssh_target, project = dk_info_fetch_media_defaults(config, dk_env)
            env["FETCH_MEDIA_SSH_HOST"] = ssh_target
            env["FETCH_COMPOSE_PROJECT_NAME"] = (
                compose_project if compose_project is not None else project
            )
        else:
            env["FETCH_MEDIA_SSH_HOST"] = host
            if compose_project is not None:
                env["FETCH_COMPOSE_PROJECT_NAME"] = compose_project
            else:
                env.pop("FETCH_COMPOSE_PROJECT_NAME", None)
    return env
