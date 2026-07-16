"""Load merged compose env from ``docker/envs/<env>/info.yaml`` + credentials (SOPS)."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from catalpa_tooling.backup_logging_env import apply_backup_logging_env
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dk_stack import vite_build_metadata_env
from catalpa_tooling.env_yaml import _credentials_to_env, _yaml_mapping_to_env
from catalpa_tooling.images import _default_image_tag, _image_registry_from_config, _load_images_config
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.site_origin import (
    domain_env_from_origins,
    parse_site_origins_from_info,
    primary_site_origin_for_env,
    primary_site_origin_from_info,
    resolve_site_origins_for_env,
    site_origin_from_info,
)
from catalpa_tooling.storage_config import StorageVolumeSpec, parse_storage_volumes_from_info

__all__ = ["site_origin_from_info"]


def _info_image_tag(info: dict) -> str | None:
    """Non-empty ``image_tag`` from ``docker/envs/<env>/info.yaml`` only."""
    if "image_tag" not in info:
        return None
    raw = info.get("image_tag")
    if raw is None or raw is False:
        return None
    s = str(raw).strip()
    return s or None


def _cli_image_tag_override(raw: str | None) -> str | None:
    """Non-empty ``--tag`` from ``dk <env>`` (whitespace stripped); empty or unset → no override."""
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _effective_deploy_image_tag(info: dict, tag_override: str | None) -> str | None:
    """Tag for stack images: CLI ``--tag`` wins over ``info.yaml`` ``image_tag``."""
    o = _cli_image_tag_override(tag_override)
    if o is not None:
        return o
    return _info_image_tag(info)


def _resolve_image_registry(info: dict, images_config: dict, config: ProjectConfig) -> str:
    if "image_registry" in info:
        raw = info["image_registry"]
        if raw is None or raw is False:
            return ""
        return str(raw).rstrip("/")
    return _image_registry_from_config(images_config, config)


def _resolve_compose_file_from_info(info: dict, config: ProjectConfig) -> str | None:
    """Return compose file path relative to repo root, or None if invalid."""
    raw = info.get("compose_file")
    if raw is None or raw is False:
        return config.compose_prod
    rel = str(raw).strip()
    if not rel or ".." in Path(rel).parts:
        print(f"Invalid compose_file in info.yaml: {raw!r}", file=sys.stderr)
        return None
    repo_root = config.repo_root
    path = (repo_root / rel).resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError:
        print(f"compose_file must stay under the repository: {rel!r}", file=sys.stderr)
        return None
    if not path.is_file():
        print(f"compose_file not found: {path}", file=sys.stderr)
        return None
    return rel


@dataclass(frozen=True)
class ManagedDeployContext:
    """Resolved deploy settings for ``dk <env>`` (compose file + env for docker compose)."""

    env_name: str
    compose_file: str
    env_add: dict[str, str]
    docker_host: str
    backup_docker_host: str
    site_origin: str
    site_origins: tuple[str, ...]
    use_prepulled_registry: bool
    image_registry: str
    info_tag: str | None
    config: ProjectConfig
    storage_volumes: dict[str, StorageVolumeSpec]


def print_managed_deploy_summary(ctx: ManagedDeployContext) -> None:
    """Log DOCKER_HOST / SITE_ORIGIN (and registry) to stderr, matching ``dk <env>`` behavior."""
    dh, origin = ctx.docker_host, ctx.site_origin
    ir, it = ctx.image_registry, ctx.info_tag
    if ir:
        if it:
            print(
                f"DOCKER_HOST={dh} SITE_ORIGIN={origin} (registry: {ir}, tag: {it})",
                file=sys.stderr,
            )
        else:
            print(
                f"DOCKER_HOST={dh} SITE_ORIGIN={origin} (registry: {ir}; "
                "set image_tag in this info.yaml or pass --tag to pin a tag and use pre-built images / --pull always)",
                file=sys.stderr,
            )
    else:
        print(f"DOCKER_HOST={dh} SITE_ORIGIN={origin}", file=sys.stderr)


def print_managed_deploy_header(
    config: ProjectConfig,
    env_name: str,
    info: dict,
    compose_file: str,
    *,
    tag_override: str | None = None,
) -> None:
    """Print DOCKER_HOST / SITE_ORIGIN summary without loading credentials (dry-run peek)."""
    images_config = _load_images_config(config)
    image_registry = _resolve_image_registry(info, images_config, config)
    effective_tag = _effective_deploy_image_tag(info, tag_override)
    use_prepulled = bool(image_registry) and effective_tag is not None
    print_managed_deploy_summary(
        ManagedDeployContext(
            env_name=env_name,
            compose_file=compose_file,
            env_add={},
            docker_host=str(info.get("docker_host", "")),
            backup_docker_host=str(info.get("backup_docker_host", "") or ""),
            site_origin=primary_site_origin_for_env(info, config, env_name),
            site_origins=tuple(resolve_site_origins_for_env(info, config, env_name)),
            use_prepulled_registry=use_prepulled,
            image_registry=image_registry,
            info_tag=effective_tag,
            config=config,
            storage_volumes={},
        )
    )


def load_managed_deploy_context(
    config: ProjectConfig,
    env_name: str,
    *,
    info: dict | None = None,
    compose_file: str | None = None,
    tag_override: str | None = None,
) -> ManagedDeployContext | None:
    """Load ``info.yaml`` + ``credentials.yaml`` and build ``env_add`` for docker compose.

    If ``info`` is None, reads ``docker/envs/<env_name>/info.yaml``.
    If ``compose_file`` is None, resolves it from ``info``.
    Prints summary to stderr. Returns None on missing files / SOPS failure.
    """
    repo_root = config.repo_root
    deploy_dir = config.deploy_envs_dir / env_name
    info_path = deploy_dir / "info.yaml"
    creds_path = deploy_dir / "credentials.yaml"

    if info is None:
        if not info_path.is_file():
            print(f"Missing {info_path}", file=sys.stderr)
            return None
        with open(info_path, encoding="utf-8") as f:
            info = yaml.safe_load(f) or {}

    if compose_file is None:
        compose_file = _resolve_compose_file_from_info(info, config)
        if compose_file is None:
            return None

    site_origins_list = resolve_site_origins_for_env(info, config, env_name)
    site_origin = site_origins_list[0] if site_origins_list else ""
    site_origins = tuple(site_origins_list)
    docker_host = info.get("docker_host", "")
    backup_docker_host = str(info.get("backup_docker_host", "") or "").strip()

    from catalpa_tooling.ssh_known_hosts import ensure_ssh_known_host_for_docker_host

    kh_rc = ensure_ssh_known_host_for_docker_host(
        str(docker_host or ""),
        recovery_env_name=env_name,
    )
    if kh_rc != 0:
        print(
            f"Could not register SSH host key for {docker_host!r}. "
            "Remote `dk` commands need the deploy host in ~/.ssh/known_hosts.",
            file=sys.stderr,
        )
        return None

    images_config = _load_images_config(config)
    image_registry = _resolve_image_registry(info, images_config, config)
    effective_tag = _effective_deploy_image_tag(info, tag_override)
    use_prepulled_registry = bool(image_registry) and effective_tag is not None

    print_managed_deploy_header(
        config, env_name, info, compose_file, tag_override=tag_override
    )

    creds: dict = {}
    if not creds_path.is_file():
        if config.credentials_optional_for_env(env_name):
            print(
                f"Note: {creds_path} not found; using env from info.yaml only "
                f"(credentials optional for this environment per tooling.yaml).",
                file=sys.stderr,
            )
        else:
            print(f"Missing {creds_path}", file=sys.stderr)
            return None
    else:
        decrypt_optional = bool(info.get("credentials_decrypt_optional", False))
        result = run_cmd(
            ["sops", "-d", str(creds_path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            print_cmd=False,
        )
        if result.returncode != 0:
            if decrypt_optional:
                print(
                    f"Warning: could not decrypt {creds_path} (sops). "
                    "Continuing with no credential keys from that file; only non-secret `env` "
                    "from info.yaml is applied. Backup/restore commands that need those secrets "
                    "will fail until decryption works (e.g. configure SOPS age key). "
                    f"sops: {result.stderr or result.stdout}",
                    file=sys.stderr,
                )
                creds = {}
            else:
                print(f"sops decrypt failed: {result.stderr or result.stdout}", file=sys.stderr)
                return None
        else:
            creds = yaml.safe_load(result.stdout) or {}

    info_env = info.get("env") or {}
    if not isinstance(info_env, dict):
        info_env = {}
    env_add = _yaml_mapping_to_env(info_env, skip_sops=False)
    env_add.update(_credentials_to_env(creds))
    env_add.update({"DOCKER_HOST": str(docker_host)})
    if "DJANGO_DEBUG" not in env_add:
        env_add["DJANGO_DEBUG"] = "0"

    if site_origin:
        env_add["SITE_ORIGIN"] = site_origin
        env_add.setdefault("BERO_ORIGIN", site_origin)
    domain_s = domain_env_from_origins(site_origins_list)
    if domain_s:
        env_add["DOMAIN"] = domain_s

    from catalpa_tooling.caddy_site_addresses import apply_caddy_site_addresses
    from catalpa_tooling.local_proxy import local_proxy_enabled

    apply_caddy_site_addresses(
        env_add,
        info=info,
        config=config,
        env_name=env_name,
        site_origin=site_origin,
        site_origins=site_origins_list,
        behind_local_proxy=local_proxy_enabled(info),
    )

    if effective_tag:
        env_add["STACK_IMAGE_TAG"] = effective_tag
    elif not use_prepulled_registry:
        cfg_tag = images_config.get("image_tag")
        if cfg_tag is not None and str(cfg_tag).strip():
            env_add["STACK_IMAGE_TAG"] = str(cfg_tag).strip()
        else:
            env_add["STACK_IMAGE_TAG"] = _default_image_tag(repo_root)

    reg_from_images_yaml = _image_registry_from_config(images_config, config).strip().rstrip("/")
    if image_registry:
        env_add["STACK_IMAGE_REGISTRY"] = image_registry
    elif reg_from_images_yaml:
        env_add["STACK_IMAGE_REGISTRY"] = reg_from_images_yaml

    if image_registry and effective_tag:
        env_add["Django_IMAGE"] = f"{image_registry}/{config.image_component('web')}:{effective_tag}"
        env_add["Caddy_IMAGE"] = f"{image_registry}/{config.image_component('proxy')}:{effective_tag}"
        env_add["Postgres_IMAGE"] = f"{image_registry}/{config.image_component('db')}:{effective_tag}"

    yaml_tag = images_config.get("image_tag")
    yaml_tag_s = str(yaml_tag).strip() if yaml_tag not in (None, False) else ""
    release_for_bundle = env_add.get("STACK_IMAGE_TAG") or yaml_tag_s or _default_image_tag(repo_root)
    env_add.update(vite_build_metadata_env(config, str(release_for_bundle)))
    apply_backup_logging_env(env_add, config, info)

    from catalpa_tooling.docker_host_tls import apply_inferred_backup_ca_file

    apply_inferred_backup_ca_file(env_add, config, env_name)

    from catalpa_tooling.dev_lan_access import build_dev_lan_env, dev_lan_access_enabled, print_dev_lan_urls

    if dev_lan_access_enabled(info):
        lan_env = build_dev_lan_env(info, config=config, env_name=env_name)
        merge_keys = (
            "BERO_EXTRA_ALLOWED_HOSTS",
            "BERO_EXTRA_ORIGINS",
            "DOMAIN",
            "VITE_EXTRA_ALLOWED_HOSTS",
        )
        for key in merge_keys:
            if key not in lan_env:
                continue
            existing = (env_add.get(key) or "").strip()
            if existing:
                seen: set[str] = set()
                merged: list[str] = []
                for part in re.split(r"[, ]+", existing):
                    part = part.strip()
                    if part and part not in seen:
                        seen.add(part)
                        merged.append(part)
                for part in re.split(r"[, ]+", lan_env[key]):
                    part = part.strip()
                    if part and part not in seen:
                        seen.add(part)
                        merged.append(part)
                lan_env[key] = ", ".join(merged)
        env_add.update(lan_env)
        if lan_env:
            print_dev_lan_urls(info)

    try:
        storage_volumes = parse_storage_volumes_from_info(info, config)
    except Exception as exc:
        from catalpa_tooling.storage_config import StorageConfigError

        if isinstance(exc, StorageConfigError):
            print(str(exc), file=sys.stderr)
            return None
        raise

    return ManagedDeployContext(
        env_name=env_name,
        compose_file=compose_file,
        env_add=dict(env_add),
        docker_host=str(docker_host),
        backup_docker_host=backup_docker_host,
        site_origin=site_origin,
        site_origins=site_origins,
        use_prepulled_registry=use_prepulled_registry,
        image_registry=image_registry,
        info_tag=effective_tag,
        config=config,
        storage_volumes=storage_volumes,
    )


def resolve_compose_file_from_info(info: dict, config: ProjectConfig) -> str | None:
    """Public alias for compose file resolution (same rules as ``dk <env>``)."""
    return _resolve_compose_file_from_info(info, config)
