"""dk push: build compose.yml stack then push images to a registry (local workstation)."""

import argparse
import sys

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dk_stack import (
    compose_yml_build,
    env_for_stack_build,
    push_registry_images,
    registry_refs,
    tag_local_to_registry,
)
from catalpa_tooling.images import _default_image_tag, _image_registry_from_config, _load_images_config


def _cmd_push(_compose_file: str, ns: argparse.Namespace, config: ProjectConfig) -> int:
    """Build compose.yml stack for linux/amd64 and push to a registry (e.g. GHCR). Not host-specific."""
    del _compose_file
    images_config = _load_images_config(config)

    registry = (
        getattr(ns, "registry", None)
        or _image_registry_from_config(images_config, config).rstrip("/")
    )
    if not registry:
        print(
            f"Set {config.stack.images.registry_key} in {config.paths.deploy.images_config} "
            "or pass --registry",
            file=sys.stderr,
        )
        return 1
    tag = (
        getattr(ns, "tag", None)
        or images_config.get("image_tag")
        or _default_image_tag(config.repo_root)
    )
    tag = str(tag).strip() if tag else _default_image_tag(config.repo_root)
    env_add = env_for_stack_build(
        config,
        image_registry=registry,
        image_tag=tag,
    )
    if compose_yml_build(config, env_add=env_add if env_add else None) != 0:
        return 1
    if tag_local_to_registry(
        config,
        registry,
        tag,
        env_add=env_add if env_add else None,
    ) != 0:
        return 1
    return push_registry_images(config, registry, tag)
