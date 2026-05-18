"""dk build: build compose.yml stack images locally."""

import argparse

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dk_stack import compose_yml_build, env_for_stack_build, maybe_tag_registry_from_config


def _cmd_build(_compose_file: str, ns: argparse.Namespace, config: ProjectConfig) -> int:
    """Build compose stack only; optional registry tags from images config."""
    del _compose_file
    env_add = env_for_stack_build(config)
    build_services = tuple(ns.services) if getattr(ns, "services", None) else None
    if compose_yml_build(config, env_add=env_add or None, services=build_services) != 0:
        return 1
    return maybe_tag_registry_from_config(config, services=build_services)
