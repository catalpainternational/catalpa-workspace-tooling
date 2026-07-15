"""dk push: build compose.yml stack then push images to a registry (local workstation)."""

import argparse
import sys

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dk_stack import (
    compose_yml_build,
    env_for_stack_build,
    push_registry_images,
    tag_local_to_registry,
)
from catalpa_tooling.images import _default_image_tag, _image_registry_from_config, _load_images_config


def push_images(
    config: ProjectConfig,
    *,
    registry: str | None = None,
    tag: str | None = None,
    sbom: bool = True,
) -> int:
    """Build compose stack, tag for registry, push, and attach image SBOMs unless disabled."""
    images_config = _load_images_config(config)

    resolved_registry = (registry or _image_registry_from_config(images_config, config)).rstrip("/")
    if not resolved_registry:
        print(
            f"Set {config.stack.images.registry_key} in {config.paths.deploy.images_config} "
            "or pass --registry",
            file=sys.stderr,
        )
        return 1
    resolved_tag = tag or images_config.get("image_tag") or _default_image_tag(config.repo_root)
    resolved_tag = str(resolved_tag).strip() if resolved_tag else _default_image_tag(config.repo_root)
    env_add = env_for_stack_build(
        config,
        image_registry=resolved_registry,
        image_tag=resolved_tag,
    )
    if compose_yml_build(config, env_add=env_add if env_add else None) != 0:
        return 1
    if tag_local_to_registry(
        config,
        resolved_registry,
        resolved_tag,
        env_add=env_add if env_add else None,
    ) != 0:
        return 1
    return push_registry_images(config, resolved_registry, resolved_tag, sbom=sbom)


def _cmd_push(_compose_file: str, ns: argparse.Namespace, config: ProjectConfig) -> int:
    """Build compose.yml stack for linux/amd64 and push to a registry (e.g. GHCR). Not host-specific."""
    del _compose_file
    return push_images(
        config,
        registry=getattr(ns, "registry", None),
        tag=getattr(ns, "tag", None),
        sbom=not bool(getattr(ns, "no_sbom", False)),
    )
