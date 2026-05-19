"""Link ``dk`` deploy environments to DigitalOcean droplets via ``info.yaml``."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.config import ProjectConfig

DEFAULT_SSH_USER = "root"
_INFO_DO_EXAMPLE = """\
digitalocean:
  droplet_name: my-hostname
  ssh_user: root   # optional
"""


@dataclass(frozen=True)
class EnvDoLink:
    droplet_name: str
    ssh_user: str = DEFAULT_SSH_USER


def public_ipv4(droplet: dict[str, Any]) -> str:
    networks = droplet.get("networks") if isinstance(droplet.get("networks"), dict) else {}
    v4 = networks.get("v4") or []
    if not isinstance(v4, list):
        return ""
    for entry in v4:
        if isinstance(entry, dict) and entry.get("type") == "public":
            return str(entry.get("ip_address") or "")
    return ""


def private_ipv4(droplet: dict[str, Any]) -> str:
    networks = droplet.get("networks") if isinstance(droplet.get("networks"), dict) else {}
    v4 = networks.get("v4") or []
    if not isinstance(v4, list):
        return ""
    for entry in v4:
        if isinstance(entry, dict) and entry.get("type") == "private":
            return str(entry.get("ip_address") or "")
    return ""


def droplet_region_slug(droplet: dict[str, Any]) -> str:
    region = droplet.get("region")
    if isinstance(region, dict):
        return str(region.get("slug") or region.get("name") or "")
    return str(region or "")


def read_env_do_link(info: dict[str, Any]) -> EnvDoLink | None:
    """Parse ``digitalocean`` block from ``docker/envs/<env>/info.yaml``."""
    raw = info.get("digitalocean")
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("droplet_name") or "").strip()
    if not name:
        return None
    ssh_user = str(raw.get("ssh_user") or DEFAULT_SSH_USER).strip() or DEFAULT_SSH_USER
    return EnvDoLink(droplet_name=name, ssh_user=ssh_user)


def format_docker_host(ssh_user: str, ip: str) -> str:
    user = (ssh_user or DEFAULT_SSH_USER).strip() or DEFAULT_SSH_USER
    host = ip.strip()
    if not host:
        return ""
    return f"ssh://{user}@{host}"


def find_droplet_by_name(name: str, *, context: str | None) -> dict[str, Any] | None:
    """Return the first droplet dict whose name matches ``name`` (case-insensitive)."""
    from catalpa_tooling.doctl_binary import run_doctl_json

    target = name.strip().lower()
    if not target:
        return None
    data = run_doctl_json(["compute", "droplet", "list"], context=context)
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        existing = str(item.get("name", "")).strip().lower()
        if existing == target:
            return item
    return None


def droplet_name_to_env_map(config: ProjectConfig) -> dict[str, str]:
    """Map ``digitalocean.droplet_name`` (lower) → deploy env directory name."""
    out: dict[str, str] = {}
    for env_name in _list_env_names(config):
        info_path = config.deploy_envs_dir / env_name / "info.yaml"
        if not info_path.is_file():
            continue
        with open(info_path, encoding="utf-8") as f:
            info = yaml.safe_load(f) or {}
        link = read_env_do_link(info if isinstance(info, dict) else {})
        if link:
            out[link.droplet_name.lower()] = env_name
    return out


def env_for_droplet_name(droplet_name: str, config: ProjectConfig) -> str:
    return droplet_name_to_env_map(config).get(droplet_name.strip().lower(), "")


def _list_env_names(config: ProjectConfig) -> list[str]:
    from catalpa_tooling.remote_deploy import list_deploy_env_names

    return list_deploy_env_names(config.deploy_envs_dir)


def load_env_info(config: ProjectConfig, env_name: str) -> tuple[Path, dict[str, Any]] | None:
    info_path = config.deploy_envs_dir / env_name / "info.yaml"
    if not info_path.is_file():
        print(f"Missing {info_path}", file=sys.stderr)
        return None
    with open(info_path, encoding="utf-8") as f:
        info = yaml.safe_load(f) or {}
    if not isinstance(info, dict):
        info = {}
    return info_path, info


def patch_info_docker_host(
    info_path: Path,
    docker_host: str,
    *,
    dry_run: bool = False,
) -> int:
    with open(info_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        data = {}
    data["docker_host"] = docker_host
    if dry_run:
        print(f"dry-run: would set docker_host in {info_path}:", file=sys.stderr)
        print(f"  docker_host: {docker_host}", file=sys.stderr)
        return 0
    with open(info_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    print(f"Updated {info_path}", file=sys.stderr)
    return 0


def print_host_resolution(
    *,
    env_name: str,
    link: EnvDoLink,
    droplet: dict[str, Any],
    docker_host: str,
) -> None:
    status = str(droplet.get("status", ""))
    droplet_id = droplet.get("id", "")
    region = droplet_region_slug(droplet)
    pub = public_ipv4(droplet)
    print(f"docker_host: {docker_host}")
    print(
        f"Droplet {link.droplet_name!r} (id {droplet_id}, {region}, {status}, public {pub})",
        file=sys.stderr,
    )
    if status and status != "active":
        print(
            f"Warning: droplet status is {status!r}; wait until active before deploying.",
            file=sys.stderr,
        )


def suggest_host_write_command(env_name: str) -> str:
    return f"dk {env_name} host --write"


def cmd_env_host(
    config: ProjectConfig,
    env_name: str,
    *,
    write: bool = False,
    dry_run: bool = False,
) -> int:
    """Resolve droplet IP for ``env_name`` and print or patch ``docker_host`` in info.yaml."""
    loaded = load_env_info(config, env_name)
    if loaded is None:
        return 1
    info_path, info = loaded

    link = read_env_do_link(info)
    if link is None:
        print(
            f"Missing digitalocean.droplet_name in {info_path}.\n"
            f"Add for example:\n{_INFO_DO_EXAMPLE}",
            file=sys.stderr,
        )
        return 1

    do_config = config.digitalocean
    context = do_config.context if do_config else None

    from catalpa_tooling.doctl_binary import (
        DoctlCommandError,
        DoctlNotFoundError,
        ensure_doctl_available,
        print_doctl_required,
    )

    if not dry_run:
        try:
            ensure_doctl_available()
        except DoctlNotFoundError as e:
            print(
                f"dk {env_name} host requires the official doctl binary on PATH (or DOCTL_BIN).",
                file=sys.stderr,
            )
            print_doctl_required(e)
            return 1

    if dry_run and write:
        print(
            f"dry-run: would look up droplet {link.droplet_name!r} and patch {info_path}",
            file=sys.stderr,
        )
        return 0

    droplet: dict[str, Any] | None
    if dry_run:
        droplet = {
            "id": 0,
            "name": link.droplet_name,
            "status": "active",
            "region": {"slug": "dry-run"},
            "networks": {"v4": [{"type": "public", "ip_address": "203.0.113.1"}]},
        }
    else:
        try:
            droplet = find_droplet_by_name(link.droplet_name, context=context)
        except DoctlNotFoundError as e:
            print(
                f"dk {env_name} host requires the official doctl binary on PATH (or DOCTL_BIN).",
                file=sys.stderr,
            )
            print_doctl_required(e)
            return 1
        except DoctlCommandError as e:
            print(str(e), file=sys.stderr)
            return e.returncode

    if droplet is None:
        print(
            f"No DigitalOcean droplet named {link.droplet_name!r}. "
            f"Create one, e.g.:\n  dk digoc droplets create {link.droplet_name} --wait",
            file=sys.stderr,
        )
        return 1

    ip = public_ipv4(droplet)
    if not ip:
        print(
            f"Droplet {link.droplet_name!r} has no public IPv4 yet (status: {droplet.get('status')!r}).",
            file=sys.stderr,
        )
        return 1

    docker_host = format_docker_host(link.ssh_user, ip)
    print_host_resolution(
        env_name=env_name,
        link=link,
        droplet=droplet,
        docker_host=docker_host,
    )

    if write:
        return patch_info_docker_host(info_path, docker_host, dry_run=dry_run)
    return 0


def droplet_name_for_env(config: ProjectConfig, env_name: str) -> str | None:
    """Return ``digitalocean.droplet_name`` for ``env_name``, or None."""
    loaded = load_env_info(config, env_name)
    if loaded is None:
        return None
    _, info = loaded
    link = read_env_do_link(info)
    return link.droplet_name if link else None
