"""Managed Docker environments: ``docker/envs/<env>/info.yaml``, used by ``dk <env> …``."""

from __future__ import annotations

import sys
from pathlib import Path

from catalpa_tooling.cli.dk_argv import normalize_dk_env_argv as _normalize_dk_env_argv
from catalpa_tooling.cli_confirm import confirm_by_typing_env_name
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dk_stack import compose_yml_build
from catalpa_tooling.env_yaml import _yaml_mapping_to_env


def _top_level_zbx_env_from_info(info: dict | None) -> dict[str, str]:
    """``zbx_*`` may be set at info.yaml top level so they are not merged into Compose ``env:``."""
    if not isinstance(info, dict):
        return {}
    subset = {
        k: v
        for k, v in info.items()
        if isinstance(k, str) and k.startswith("zbx_")
    }
    return _yaml_mapping_to_env(subset, skip_sops=False)


def _zabbix_env_defaults(
    info: dict | None,
    env_add: dict[str, str],
) -> dict[str, str]:
    """Keys from ``info.yaml`` ``env:`` and top-level ``zbx_*``, plus ``ZBX_*`` from deploy env (credentials).

    Duplicate keys: ``env:`` overrides top-level ``zbx_*``. Credentials ``env_add`` overrides both.
    """
    env_map = info.get("env") if isinstance(info, dict) else None
    env_map = env_map if isinstance(env_map, dict) else {}
    from_env = _yaml_mapping_to_env(env_map, skip_sops=False)
    from_top = _top_level_zbx_env_from_info(info)
    out = {**from_top, **from_env}
    plaintext_psk = {**from_top, **from_env}
    for psk_key in ("ZBX_TLSPSK", "ZBX_TLSPSKIDENTITY"):
        if (plaintext_psk.get(psk_key) or "").strip():
            print(
                "zabbix: TLS PSK secrets must be set in credentials.yaml "
                f"(`zbx_tlspsk` / `zbx_tlspskidentity`), not in info.yaml ({psk_key} is set in info).",
                file=sys.stderr,
            )
            break
    for k, v in env_add.items():
        if isinstance(k, str) and k.startswith("ZBX_"):
            out[k] = str(v)
    return out


def list_deploy_env_names(deploy_envs_dir: Path) -> list[str]:
    """Directory names under ``deploy_envs_dir`` that contain ``info.yaml`` (excludes ``_*``)."""
    root = deploy_envs_dir
    if not root.is_dir():
        return []
    names: list[str] = []
    for p in sorted(root.iterdir()):
        if p.name.startswith("_") or not p.is_dir():
            continue
        if (p / "info.yaml").is_file():
            names.append(p.name)
    return names


def list_dk_env_names(config: ProjectConfig) -> list[str]:
    """Canonical deploy env names plus deprecated alias keys from tooling.yaml."""
    canonical = list_deploy_env_names(config.deploy_envs_dir)
    aliases = [name for name in config.paths.deploy.env_aliases if name not in canonical]
    return sorted(set(canonical) | set(aliases))


def resolve_deploy_env_name(config: ProjectConfig, env_name: str) -> str:
    """Return canonical env name; emit deprecation warning when ``env_name`` is an alias."""
    from catalpa_tooling.deprecation import warn_deprecated

    canonical = config.resolve_deploy_env_name(env_name)
    if canonical != env_name:
        warn_deprecated(f"dk {env_name}", f"dk {canonical}")
    return canonical


def _strip_dk_up_provision_flag(compose_args: list[str]) -> list[str]:
    """Remove dk-only ``--provision`` (volume ensure + materialize configs) before ``docker compose up``."""
    if not compose_args or compose_args[0] != "up":
        return compose_args
    return [a for a in compose_args if a != "--provision"]


def _is_compose_down_with_volumes(compose_args: list[str]) -> bool:
    """True if args run `docker compose down` with volume removal (-v / --volumes)."""
    if len(compose_args) < 1 or compose_args[0] != "down":
        return False
    return "-v" in compose_args or "--volumes" in compose_args


def _ensure_local_stack_images_built(
    config: ProjectConfig,
    env_add: dict[str, str],
    *,
    use_prepulled_registry: bool,
) -> int:
    """When not using pinned pre-pulled images, ``docker compose build`` stack images before volume init."""
    if use_prepulled_registry:
        return 0
    return compose_yml_build(config, env_add=env_add, services=None)


def _compose_up_service_index(compose_args: list[str]) -> int:
    """Index in ``up`` argv before the first service name (or ``len`` if none)."""
    if not compose_args or compose_args[0] != "up":
        return len(compose_args)
    i = 1
    flags_with_value = frozenset({"-t", "--timeout", "-p", "--profile", "--pull"})
    while i < len(compose_args):
        arg = compose_args[i]
        if not arg.startswith("-"):
            break
        i += 1
        if arg in flags_with_value and i < len(compose_args) and not compose_args[i].startswith("-"):
            i += 1
    return i


def _insert_up_prepulled_pull_flags(
    compose_args: list[str], *, use_prepulled_registry: bool
) -> list[str]:
    """When using pinned registry images, add ``--pull missing`` and ``--no-build`` before service names."""
    if not use_prepulled_registry or not compose_args or compose_args[0] != "up":
        return compose_args
    out = list(compose_args)
    inserts: list[str] = []
    if "--pull" not in out:
        inserts.extend(["--pull", "missing"])
    if "--no-build" not in out and "--build" not in out:
        inserts.append("--no-build")
    if not inserts:
        return out
    i = _compose_up_service_index(out)
    for part in reversed(inserts):
        out.insert(i, part)
    return out


def _insert_up_build_if_no_registry(
    compose_args: list[str], *, use_prepulled_registry: bool
) -> list[str]:
    """When not using pre-pulled registry images, ensure ``up`` includes ``--build`` unless opted out."""
    if use_prepulled_registry:
        return compose_args
    if not compose_args or compose_args[0] != "up":
        return compose_args
    if "--build" in compose_args or "--no-build" in compose_args:
        return compose_args
    out = list(compose_args)
    out.insert(_compose_up_service_index(out), "--build")
    return out


def _global_dry_run_runs_systemd_install(peek: list[str]) -> bool:
    """True when global ``--dry-run`` should still load credentials (systemd install via SSH)."""
    if len(peek) >= 2 and peek[1] == "install-systemd":
        return peek[0] in ("db", "bkp_db", "files", "bkp_files")
    return False


def _dry_run_exits_before_compose_env(peek: list[str]) -> bool:
    """False when ``--dry-run`` should still resolve env and run (e.g. ``ensure_volumes``, systemd)."""
    if _global_dry_run_runs_systemd_install(peek):
        return False
    if len(peek) >= 1 and peek[0] in (
        "ensure_volumes",
        "storage",
        "trust-caddy-cert",
        "pull_media",
        "zabbix",
    ):
        return False
    if len(peek) >= 2 and peek[0] in ("files", "bkp_files") and peek[1] == "push":
        return False
    return True


def _confirm_deploy_wipe(env_name: str, site_origin: str, docker_host: str) -> bool:
    """Interactive guard: user must type the environment name."""
    print(
        "WARNING: This will run `docker compose down -v` on the deployment host, then remove "
        "external volumes for PostgreSQL data and Django media (database + uploads). "
        "Other external volumes (caddy_data, postgres_conf, pgbackrest_conf) are not removed.",
        file=sys.stderr,
    )
    print(f"  Environment: {env_name}", file=sys.stderr)
    print(f"  Site origin: {site_origin or '(none)'}", file=sys.stderr)
    print(f"  DOCKER_HOST: {docker_host or '(default local socket)'}", file=sys.stderr)
    return confirm_by_typing_env_name(env_name)
