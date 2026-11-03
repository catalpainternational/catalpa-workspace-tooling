"""``dk context``: names, origins and compose identity for agents (main checkout or worktree).

Static by default: reads ``tooling.yaml``, ``info.yaml`` and the worktree marker only. No SOPS
decrypt, no ssh, no docker, so an agent can call it first. ``--status`` adds a ``docker ps`` per
local env. Values set only in ``credentials.yaml`` are not visible here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.env_yaml import _yaml_mapping_to_env
from catalpa_tooling.local_proxy import docker_endpoint_is_local, local_proxy_enabled
from catalpa_tooling.managed_deploy_env import apply_worktree_overlay_for_env
from catalpa_tooling.remote_deploy import list_deploy_env_names
from catalpa_tooling.restic_files import resolve_env_with_compose_project
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.site_origin import (
    django_site_hosts_from_env,
    hostnames_from_origins,
    normalize_site_origin_entry,
    parse_redirect_origins_from_info,
    resolve_site_origins_for_env,
)
from catalpa_tooling.worktree_overlay import WorktreeOverlayError, load_worktree_overlay

CONTEXT_SCHEMA_VERSION = 1


def attach_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env",
        dest="context_env",
        default=None,
        metavar="NAME",
        help="Report only this env (default: every env with an info.yaml).",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable output on stdout.")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Also list each local env's containers (runs docker ps).",
    )


def _git_output(repo_root: Path, *args: str) -> str | None:
    proc = run_cmd(
        ["git", *args],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
        print_cmd=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _load_info(config: ProjectConfig, env_name: str) -> dict[str, Any]:
    path = config.deploy_envs_dir / env_name / "info.yaml"
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping")
    return raw


def _env_origins(info: dict[str, Any], config: ProjectConfig, env_name: str) -> list[str]:
    """App origins plus ``DJANGO_EXTRA_ORIGINS`` hosts from ``info.env``, deduped by host."""
    origins = resolve_site_origins_for_env(info, config, env_name)
    seen = set(hostnames_from_origins(origins))
    env_block = info.get("env")
    if isinstance(env_block, dict):
        for host in django_site_hosts_from_env(_yaml_mapping_to_env(env_block)):
            if host not in seen:
                origins.append(normalize_site_origin_entry(host))
                seen.add(host)
    return origins


def _compose_project(info: dict[str, Any], config: ProjectConfig, env_name: str) -> str:
    """What docker compose will use: explicit ``env.compose_project_name`` or the derived default."""
    env_block = info.get("env")
    env_add = _yaml_mapping_to_env(env_block) if isinstance(env_block, dict) else {}
    env_add["DOCKER_HOST"] = str(info.get("docker_host") or "")
    resolved = resolve_env_with_compose_project(
        "", env_add, dk_env_name=env_name, config=config
    )
    return resolved["COMPOSE_PROJECT_NAME"]


def _stack_status(compose_project: str, docker_host: str) -> dict[str, Any]:
    env = dict(os.environ)
    if docker_host:
        env["DOCKER_HOST"] = docker_host
    try:
        proc = run_cmd(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={compose_project}",
                "--format",
                '{{.Label "com.docker.compose.service"}}\t{{.State}}',
            ],
            check=False,
            capture_output=True,
            text=True,
            print_cmd=False,
            env=env,
        )
    except OSError as exc:
        return {"error": str(exc)}
    if proc.returncode != 0:
        return {"error": (proc.stderr or "").strip() or f"docker ps exited {proc.returncode}"}
    running: list[str] = []
    stopped: list[str] = []
    for line in (proc.stdout or "").splitlines():
        service, _, state = line.partition("\t")
        if not service:
            continue
        (running if state.strip() == "running" else stopped).append(service)
    return {"running": sorted(set(running)), "stopped": sorted(set(stopped) - set(running))}


def _env_context(
    config: ProjectConfig, env_name: str, *, status: bool
) -> dict[str, Any]:
    out: dict[str, Any] = {"name": env_name}
    try:
        info = _load_info(config, env_name)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        out["error"] = str(exc)
        return out
    ok, info, _overlay, applied = apply_worktree_overlay_for_env(
        info, config.repo_root, env_name
    )
    docker_host = str(info.get("docker_host") or "").strip()
    local = docker_endpoint_is_local(docker_host)
    out.update(
        {
            "location": "local" if local else "remote",
            "docker_host": docker_host,
            "compose_project": _compose_project(info, config, env_name),
            "origins": _env_origins(info, config, env_name),
            "redirect_origins": parse_redirect_origins_from_info(info),
            "local_proxy": local_proxy_enabled(info),
            "worktree_overlay": bool(ok and applied),
        }
    )
    if status and local:
        out["status"] = _stack_status(out["compose_project"], docker_host)
    return out


def _worktree_context(config: ProjectConfig) -> dict[str, Any] | None:
    overlay = load_worktree_overlay(config.repo_root)
    if overlay is None:
        return None
    return {
        "slug": overlay.slug,
        "base_env": overlay.base_env,
        "path": str(config.repo_root.resolve()),
        "parent_repo_root": overlay.parent_repo_root,
        "branch": _git_output(config.repo_root, "rev-parse", "--abbrev-ref", "HEAD")
        or overlay.branch,
        "head_commit": _git_output(config.repo_root, "rev-parse", "HEAD"),
        "base_ref": overlay.base_ref,
        "base_commit": overlay.base_commit,
    }


def build_context(
    config: ProjectConfig,
    *,
    env: str | None = None,
    status: bool = False,
) -> dict[str, Any]:
    """Return the ``dk context`` payload. Raises ``ValueError`` for an unknown ``env``."""
    env_names = list_deploy_env_names(config.deploy_envs_dir) if config.has_deploy_paths else []
    if env:
        canonical = config.resolve_deploy_env_name(env) if config.has_deploy_paths else env
        if canonical not in env_names:
            known = ", ".join(env_names) or "none"
            raise ValueError(f"unknown env {env!r} (known: {known})")
        env_names = [canonical]
    worktree = _worktree_context(config)
    return {
        "schema": CONTEXT_SCHEMA_VERSION,
        "project": config.meta.name,
        "repo_root": str(config.repo_root.resolve()),
        "checkout": "worktree" if worktree else "main",
        "worktree": worktree,
        "web_service": config.stack_service("web") if config.has_stack else None,
        "envs": [_env_context(config, name, status=status) for name in env_names],
    }


def _print_human(payload: dict[str, Any]) -> None:
    print(f"project: {payload['project']}")
    print(f"checkout: {payload['checkout']} ({payload['repo_root']})")
    wt = payload["worktree"]
    if wt:
        print(f"worktree: {wt['slug']} (base_env {wt['base_env']}, branch {wt['branch'] or '-'})")
        base = wt["base_ref"] or wt["base_commit"] or "not recorded"
        print(f"  cut from: {base}")
    print(f"web_service: {payload['web_service'] or '-'}")
    for env in payload["envs"]:
        if "error" in env:
            print(f"{env['name']}: error: {env['error']}")
            continue
        where = env["location"] + (f" {env['docker_host']}" if env["docker_host"] else "")
        overlay = ", worktree overlay" if env["worktree_overlay"] else ""
        print(f"{env['name']}: {where}, compose project {env['compose_project']}{overlay}")
        for origin in env["origins"]:
            print(f"  {origin}")
        state = env.get("status")
        if state is not None:
            if "error" in state:
                print(f"  status: error: {state['error']}")
            else:
                print(
                    f"  running: {', '.join(state['running']) or '-'}; "
                    f"stopped: {', '.join(state['stopped']) or '-'}"
                )


def cmd_context(ns: argparse.Namespace, config: ProjectConfig) -> int:
    try:
        payload = build_context(config, env=ns.context_env, status=bool(ns.status))
    except (ValueError, WorktreeOverlayError) as exc:
        print(f"dk context: {exc}", file=sys.stderr)
        return 1
    if ns.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_human(payload)
    return 0
