"""Rsync media from a deploy host (Docker volume or legacy host path)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.restic_files import django_media_volume_name
from catalpa_tooling.run_cmd import format_shell_command, run as run_cmd
from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

# Like ``rsync -az`` but omit ``-l`` (--links): symbolic links on the server are skipped.
RSYNC_BASE = ("rsync", "-rptgoDzv", "--progress")


def ssh_target_from_host(host: str) -> str:
    """Normalize bare hostname to ``root@host``; leave ``user@host`` unchanged."""
    host = host.strip()
    if "@" in host:
        return host
    return f"root@{host}"


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


def _docker_volume_mountpoint(ssh_target: str, volume_name: str) -> str:
    # Use JSON output: Go templates like ``{{ .Mountpoint }}`` break when ssh joins
    # argv without quoting (remote shell sees unclosed ``{{``).
    cmd = ["ssh", ssh_target, "docker", "volume", "inspect", volume_name]
    print(f"$ {format_shell_command(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        print(
            f"fetch media: docker volume inspect failed for {volume_name!r} on {ssh_target}.",
            file=sys.stderr,
        )
        if err:
            print(err, file=sys.stderr)
        raise SystemExit(proc.returncode or 1)
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        print(
            f"fetch media: invalid JSON from docker volume inspect for {volume_name!r}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    if not isinstance(payload, list) or not payload:
        print(
            f"fetch media: volume {volume_name!r} not found on {ssh_target}.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    first = payload[0]
    if not isinstance(first, dict):
        print(f"fetch media: unexpected inspect payload for {volume_name!r}.", file=sys.stderr)
        raise SystemExit(1)
    mount = str(first.get("Mountpoint") or "").strip()
    if not mount:
        print(
            f"fetch media: empty Mountpoint from docker volume inspect for {volume_name!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return mount.rstrip("/")


def _run_rsync(ssh_target: str, remote_path: str, local_path: Path) -> None:
    local_path.mkdir(parents=True, exist_ok=True)
    remote = f"{ssh_target}:{remote_path}"
    cmd = [*RSYNC_BASE, remote, str(local_path)]
    print(f"$ {format_shell_command(cmd)}", file=sys.stderr)
    run_cmd(cmd, check=True)


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
    """Sync media into ``dest`` (default: ``dev.fetch_media.dest`` under repo root)."""
    if not shutil.which("rsync"):
        print("rsync is not installed or not on PATH.", file=sys.stderr)
        raise SystemExit(1)
    if not shutil.which("ssh"):
        print("ssh is not installed or not on PATH.", file=sys.stderr)
        raise SystemExit(1)

    local_base = (dest if dest is not None else config.fetch_media_dest_path).resolve()

    if legacy_path:
        legacy = config.dev.fetch_media.legacy
        remote_base = (legacy_remote or (legacy.remote if legacy else "")).rstrip("/")
        if not remote_base:
            raise ValueError(
                "legacy path mode requires dev.fetch_media.legacy.remote in tooling.yaml "
                "or --remote PATH"
            )
        ssh_host = host
        if ssh_host is None and legacy and legacy.ssh_host:
            ssh_host = legacy.ssh_host
        if not ssh_host:
            raise ValueError(
                "legacy path mode requires --host USER@HOST or dev.fetch_media.legacy.ssh_host "
                "in tooling.yaml"
            )
        ssh_target = ssh_target_from_host(ssh_host)
        print(f"Rsync legacy path {remote_base!r} from {ssh_target} …", file=sys.stderr)
    else:
        if host is None:
            ssh_target, project = dk_info_fetch_media_defaults(config, dk_env)
        else:
            ssh_target = ssh_target_from_host(host)
            if compose_project is not None:
                project = compose_project
            else:
                _, project = dk_info_fetch_media_defaults(config, dk_env)
        volume = django_media_volume_name(
            compose_project if compose_project is not None else project,
            config=config,
        )
        print(f"Resolving Docker volume {volume!r} on {ssh_target} …", file=sys.stderr)
        remote_base = _docker_volume_mountpoint(ssh_target, volume)

    if partial:
        for sub in ("documents", "original_images"):
            _run_rsync(ssh_target, f"{remote_base}/{sub}/", local_base / sub)
    else:
        _run_rsync(ssh_target, f"{remote_base}/", local_base)

    print(f"Done: {local_base}", file=sys.stderr)
