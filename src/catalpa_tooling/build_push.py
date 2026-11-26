"""dk push: build compose.yml stack then push images to a registry (local workstation)."""

import argparse
import json
import os
import re
import sys

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dk_stack import (
    compose_yml_build,
    env_for_stack_build,
    push_registry_images,
    tag_local_to_registry,
)
from catalpa_tooling.images import (
    _default_image_tag,
    _image_registry_from_config,
    _load_images_config,
    deploy_platform_from_config,
)
from catalpa_tooling.run_cmd import run as run_cmd


def _inspect_platform(image: str, env_add: dict[str, str] | None) -> str | None:
    """``<os>/<arch>`` of a local image, or ``None`` when it cannot be inspected."""
    run_env = os.environ.copy()
    if env_add:
        run_env.update(env_add)
    r = run_cmd(
        ["docker", "image", "inspect", image, "--format", "{{.Os}}/{{.Architecture}}"],
        env=run_env,
        check=False,
        capture_output=True,
        text=True,
        print_cmd=False,
    )
    if r.returncode != 0:
        return None
    value = (r.stdout or "").strip().splitlines()
    if not value:
        return None
    platform = value[0].strip()
    return platform if platform.count("/") == 1 else None


def verify_built_image_platforms(
    config: ProjectConfig,
    env_add: dict[str, str] | None = None,
    *,
    deploy_platform: str | None = None,
) -> int:
    """0 when every built stack image matches the platform it is meant for (#114, #138).

    With ``deploy_platform`` (what ``dk push`` passes, from ``images.yaml``), every built image
    must be that platform, whether or not its service declares ``platform:``; a service that
    declares a different platform is also an error. Without it, each image is checked against
    its service's own ``platform:`` key only.

    ``dk push`` builds with a plain ``docker compose build`` and takes the platform entirely
    from the consumer's ``platform:`` keys. That assumption does not hold on every engine —
    against Podman's compat API compose silently drops the platform and builds native — so the
    images that reach a registry can be the wrong architecture for the servers that will run
    them, with nothing in the build or the push saying so.

    A declared platform that the image does not match is an error: ``dk push`` runs
    unattended in CI, where a warning would simply scroll past. A service with no declared
    platform is a warning naming the architecture being pushed, since the local host's
    architecture is not a safe default for a deploy artifact.
    """
    from catalpa_tooling.stale_stack import _compose_config

    resolved = _compose_config(config, config.compose_prod, env_add or {})
    services = (resolved or {}).get("services") or {}

    mismatches: list[tuple[str, str, str, str]] = []
    undeclared: list[tuple[str, str, str]] = []
    for name, svc in sorted(services.items()):
        if not isinstance(svc, dict) or not svc.get("build"):
            continue
        image = str(svc.get("image") or "").strip()
        if not image:
            continue
        actual = _inspect_platform(image, env_add)
        if actual is None:
            # The build just produced these; an image we cannot inspect is not something to
            # block a push over, and compose_yml_build already failed loudly if it broke.
            continue
        declared = str(svc.get("platform") or "").strip()
        expected = deploy_platform or declared
        if expected and expected != actual:
            mismatches.append((name, image, expected, actual))
        elif declared and deploy_platform and declared != deploy_platform:
            mismatches.append((name, image, deploy_platform, f"{actual} (declares {declared})"))
        elif not expected:
            undeclared.append((name, image, actual))

    for name, image, actual in undeclared:
        print(
            f"dk push: {name} declares no platform; pushing {actual} image {image}.\n"
            "  The host's architecture is not a safe default for a deploy artifact — pin\n"
            "  `platform:` on this service if the servers are not that architecture.",
            file=sys.stderr,
        )
    if not mismatches:
        return 0
    what = "the deploy platform" if deploy_platform else "their declared platform"
    print(f"dk push: refusing to push — built images do not match {what}.", file=sys.stderr)
    for name, image, expected, actual in mismatches:
        print(f"  {name}: {image}\n    expected {expected}, built {actual}", file=sys.stderr)
    print(
        "  The engine built a platform the compose file did not ask for. `docker compose build`\n"
        "  does not forward `platform:` on every engine (Podman's compat API drops it), so the\n"
        "  build can succeed and still produce the wrong architecture.\n"
        "  Build on an engine that honours it, or build these images with an explicit\n"
        "  `docker build --platform <platform>`.",
        file=sys.stderr,
    )
    return 1


BUILD_PLACEHOLDER = "dk-build-placeholder"
_REQUIRED_VAR = re.compile(r"required variable (\w+) is missing a value")
_MAX_PLACEHOLDERS = 100


def fill_required_placeholders(config: ProjectConfig, env_add: dict[str, str]) -> bool:
    """Give every variable compose requires (``${KEY:?…}``) and finds unset a placeholder (#211).

    A build in CI has no credentials loaded, and compose interpolates the whole file before
    building, so one required credential stops it even though the build never uses it. The
    placeholders go into ``env_add`` (this build's environment only), and their names are printed.
    A placeholder that ends up in a service's ``build.args`` would be baked into an image, so that
    build is refused (False) naming the variable.
    """
    filled: list[str] = []
    run_env = os.environ.copy()
    for _ in range(_MAX_PLACEHOLDERS):
        run_env.update(env_add)
        proc = run_cmd(
            ["docker", "compose", "-f", config.compose_prod, "config", "--format", "json"],
            cwd=config.repo_root,
            env=run_env,
            capture_output=True,
            text=True,
            check=False,
            print_cmd=False,
        )
        if proc.returncode == 0:
            break
        match = _REQUIRED_VAR.search(proc.stderr or "")
        if not match or match.group(1) in env_add:
            return True  # not a missing credential: the build itself reports it
        env_add[match.group(1)] = BUILD_PLACEHOLDER
        filled.append(match.group(1))
    else:
        return True
    if not filled:
        return True
    try:
        services = json.loads(proc.stdout or "{}").get("services") or {}
    except json.JSONDecodeError:
        services = {}
    refused: list[str] = []
    for name, svc in services.items():
        args = (svc.get("build") or {}).get("args") or {}
        if isinstance(args, list):
            args = dict(a.split("=", 1) for a in args if "=" in a)
        for arg, value in args.items():
            if value == BUILD_PLACEHOLDER:
                refused.append(f"{name}: build arg {arg}")
    if refused:
        print(
            "dk push: compose needs a real value for a variable used in build.args, "
            "and it is not set: " + ", ".join(refused) + ". Set it in the build's environment.",
            file=sys.stderr,
        )
        return False
    print(
        f"dk push: no value set for {', '.join(filled)}; using {BUILD_PLACEHOLDER!r} for the build "
        "only (required by compose.yml, not used by the build).",
        file=sys.stderr,
    )
    return True


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
    deploy_platform = deploy_platform_from_config(images_config)
    env_add = env_for_stack_build(
        config,
        image_registry=resolved_registry,
        image_tag=resolved_tag,
        stack_platform=deploy_platform,
    )
    if not fill_required_placeholders(config, env_add):
        return 1
    if compose_yml_build(config, env_add=env_add if env_add else None) != 0:
        return 1
    # Between build and tag: the last point where a wrong-architecture image is still local.
    if (
        verify_built_image_platforms(
            config, env_add if env_add else None, deploy_platform=deploy_platform
        )
        != 0
    ):
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
    """Build compose.yml for the deploy platform and push to a registry (e.g. GHCR).

    The platform is ``images.yaml`` ``deploy_platform`` (default linux/amd64), injected as
    ``STACK_PLATFORM`` and checked on every built image before anything is pushed.
    """
    del _compose_file
    return push_images(
        config,
        registry=getattr(ns, "registry", None),
        tag=getattr(ns, "tag", None),
        sbom=not bool(getattr(ns, "no_sbom", False)),
    )
