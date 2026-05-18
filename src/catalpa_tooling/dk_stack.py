"""compose.yml stack: build and registry tag/push for stack images."""

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.images import (
    _default_image_tag,
    _github_repository,
    _image_registry_from_config,
    _load_images_config,
)
from catalpa_tooling.tty_restore import restore_controlling_tty

_BUILD_TIME_TZ = ZoneInfo("Asia/Dili")


def stack_build_services(config: ProjectConfig) -> tuple[str, ...]:
    return (config.stack_service("db"), config.stack_service("web"), config.stack_service("proxy"))


def registry_refs(config: ProjectConfig, registry: str, tag: str) -> tuple[str, str, str]:
    r = registry.rstrip("/")
    return (
        f"{r}/{config.image_component('web')}:{tag}",
        f"{r}/{config.image_component('proxy')}:{tag}",
        f"{r}/{config.image_component('db')}:{tag}",
    )


def vite_build_metadata_env(config: ProjectConfig, release_tag: str) -> dict[str, str]:
    """Public ``VITE_*`` vars baked into the SPA at image build time (see compose.yml caddy args)."""
    built_at = datetime.now(_BUILD_TIME_TZ).isoformat(timespec="seconds")
    out: dict[str, str] = {
        "VITE_RELEASE": release_tag,
        "VITE_BUILD_TIME": built_at,
    }
    r = run_cmd(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        cwd=config.repo_root,
        print_cmd=False,
    )
    short = (r.stdout or "").strip() if r.returncode == 0 else ""
    out["VITE_GIT_SHA"] = short
    return out


def compose_yml_build(
    config: ProjectConfig,
    *,
    env_add: dict[str, str] | None = None,
    services: tuple[str, ...] | None = None,
) -> int:
    """Run ``docker compose -f compose.yml build`` from repo root."""
    names = services if services else stack_build_services(config)
    compose_file = config.compose_prod
    cmd = ["docker", "compose", "-f", compose_file, "build", *names]
    run_env = os.environ.copy()
    if env_add:
        run_env.update(env_add)
    try:
        return run_cmd(
            cmd,
            cwd=config.repo_root,
            env=run_env,
            check=False,
        ).returncode
    finally:
        restore_controlling_tty()


def _compose_service_images(
    config: ProjectConfig, env_add: dict[str, str] | None
) -> dict[str, str]:
    """Resolved ``image:`` for stack services (same env as ``compose build``)."""
    run_env = os.environ.copy()
    if env_add:
        run_env.update(env_add)
    svc_web = config.stack_service("web")
    svc_proxy = config.stack_service("proxy")
    svc_db = config.stack_service("db")
    proc = run_cmd(
        ["docker", "compose", "-f", config.compose_prod, "config", "--format", "json"],
        cwd=config.repo_root,
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout, file=sys.stderr)
        return {}
    data = json.loads(proc.stdout)
    svcs = data.get("services") or {}
    out: dict[str, str] = {}
    for name in (svc_db, svc_web, svc_proxy):
        img = (svcs.get(name) or {}).get("image")
        if img:
            out[name] = str(img).strip()
    return out


def _local_taggable_ref(image_declared: str) -> str | None:
    """Return a reference Docker accepts for ``docker tag <src>`` (exists locally)."""
    if not image_declared:
        return None
    candidates: list[str] = [image_declared]
    last = image_declared.rsplit("/", 1)[-1]
    if ":" not in last:
        candidates.append(f"{image_declared}:latest")
    for c in candidates:
        r = run_cmd(
            ["docker", "image", "inspect", c],
            capture_output=True,
            check=False,
            print_cmd=False,
        )
        if r.returncode == 0:
            return c
    r = run_cmd(
        ["docker", "images", "-q", image_declared],
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    ids = list(dict.fromkeys([x for x in r.stdout.strip().splitlines() if x]))
    if len(ids) == 1:
        return ids[0]
    return None


def tag_local_to_registry(
    config: ProjectConfig,
    registry: str,
    tag: str,
    *,
    env_add: dict[str, str] | None = None,
    services: tuple[str, ...] | None = None,
) -> int:
    """``docker tag`` locally built stack images to registry paths."""
    reg = registry.rstrip("/")
    resolved = _compose_service_images(config, env_add)
    svc_web = config.stack_service("web")
    svc_proxy = config.stack_service("proxy")
    svc_db = config.stack_service("db")
    mapping = (
        (svc_web, resolved.get(svc_web), f"{reg}/{config.image_component('web')}:{tag}"),
        (svc_proxy, resolved.get(svc_proxy), f"{reg}/{config.image_component('proxy')}:{tag}"),
        (svc_db, resolved.get(svc_db), f"{reg}/{config.image_component('db')}:{tag}"),
    )
    want = None if not services else set(services)
    if want is not None:
        mapping = tuple(m for m in mapping if m[0] in want)
    default_services = stack_build_services(config)
    for _svc, image_declared, dst in mapping:
        if not image_declared:
            print(
                f"compose config missing image for stack service (got: {resolved!r})",
                file=sys.stderr,
            )
            return 1
        if image_declared.strip() == dst.strip():
            continue
        src = _local_taggable_ref(image_declared)
        if not src:
            print(
                f"No local image found for compose image {image_declared!r} "
                f"(try: docker compose -f {config.compose_prod} build {' '.join(default_services)})",
                file=sys.stderr,
            )
            return 1
        r = run_cmd(["docker", "tag", src, dst], check=False)
        if r.returncode != 0:
            print(f"docker tag failed: {src} -> {dst}", file=sys.stderr)
            return r.returncode
    return 0


def push_registry_images(config: ProjectConfig, registry: str, tag: str) -> int:
    """Push the three registry-tagged images."""
    try:
        for ref in registry_refs(config, registry, tag):
            r = run_cmd(["docker", "push", ref], check=False)
            if r.returncode != 0:
                print(f"docker push failed: {ref}", file=sys.stderr)
                return r.returncode
        return 0
    finally:
        restore_controlling_tty()


def env_for_stack_build(
    config: ProjectConfig,
    *,
    github_repository: str | None = None,
    image_registry: str | None = None,
    image_tag: str | None = None,
) -> dict[str, str]:
    """Env vars for compose build: OCI labels, ``STACK_IMAGE_*`` aligned with compose.yml defaults."""
    out: dict[str, str] = {}
    gh = (github_repository or "").strip() or _github_repository(config.repo_root)
    if gh:
        out["GITHUB_REPOSITORY"] = gh
    cfg = _load_images_config(config)
    reg = (image_registry or _image_registry_from_config(cfg, config) or "").strip().rstrip("/")
    if reg:
        out["STACK_IMAGE_REGISTRY"] = reg
    tag = image_tag if image_tag is not None else cfg.get("image_tag")
    if tag is None or tag == "":
        tag = _default_image_tag(config.repo_root)
    out["STACK_IMAGE_TAG"] = str(tag)
    out.update(vite_build_metadata_env(config, str(tag)))
    return out


def maybe_tag_registry_from_config(
    config: ProjectConfig,
    *,
    services: tuple[str, ...] | None = None,
) -> int:
    """If images.yaml sets registry, tag local stack images when names differ from compose defaults."""
    cfg = _load_images_config(config)
    registry = _image_registry_from_config(cfg, config).rstrip("/")
    if not registry:
        return 0
    raw_tag = cfg.get("image_tag")
    tag = str(raw_tag) if raw_tag is not None else _default_image_tag(config.repo_root)
    env_add = env_for_stack_build(config)
    resolved = _compose_service_images(config, env_add if env_add else None)
    check = stack_build_services(config) if not services else tuple(services)
    reg = registry.rstrip("/")
    svc_web = config.stack_service("web")
    svc_proxy = config.stack_service("proxy")
    svc_db = config.stack_service("db")
    expected = {
        svc_web: f"{reg}/{config.image_component('web')}:{tag}",
        svc_proxy: f"{reg}/{config.image_component('proxy')}:{tag}",
        svc_db: f"{reg}/{config.image_component('db')}:{tag}",
    }
    if resolved and all(resolved.get(s) == expected[s] for s in check):
        print("Stack images already match registry/tag from compose config; skipping docker tag.")
        return 0
    print(f"Tagging stack images for {registry} (tag: {tag})")
    return tag_local_to_registry(
        config,
        registry,
        tag,
        env_add=env_add if env_add else None,
        services=services,
    )
