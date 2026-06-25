"""Link ``dk`` deploy environments to DigitalOcean droplets via ``info.yaml``."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from catalpa_tooling.config import ProjectConfig

DEFAULT_SSH_USER = "root"
_INFO_DO_EXAMPLE = """\
digitalocean:
  droplet_name: my-hostname   # optional; default is {project.name}-{env}
  ssh_user: root              # optional
  disabled: false             # true: manual docker_host only (no droplet / DO DNS API)
  size: s-2vcpu-4gb           # optional; for `dk <env> host create`
  region: sgp1                # optional; for `dk <env> host create`
  dns_ttl: 3600               # optional; DO A record TTL in seconds (default 300)
"""


@dataclass(frozen=True)
class EnvDoLink:
    droplet_name: str
    ssh_user: str = DEFAULT_SSH_USER
    explicit_droplet_name: bool = False
    size: str | None = None
    region: str | None = None


def normalize_droplet_hostname(name: str) -> str:
    """Return a DigitalOcean-valid droplet hostname (``_`` → ``-``)."""
    return name.replace("_", "-")


def default_droplet_name(config: ProjectConfig, env_name: str) -> str:
    """Default DigitalOcean droplet hostname: ``{project.name}-{env}`` (underscores → hyphens)."""
    return normalize_droplet_hostname(f"{config.meta.name}-{env_name}")


def _env_do_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _truthy(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, int):
        return value != 0
    return False


def is_digitalocean_host_disabled(info: dict[str, Any]) -> bool:
    """True when env ``info.yaml`` opts out of droplet lookup and DO DNS API."""
    raw = info.get("digitalocean")
    if not isinstance(raw, dict):
        return False
    return _truthy(raw.get("disabled"))


def _parse_env_do_block(raw: Any) -> tuple[str | None, str, str | None, str | None]:
    """Return ``(droplet_name, ssh_user, size, region)`` from ``info.yaml`` ``digitalocean``."""
    if not isinstance(raw, dict):
        return None, DEFAULT_SSH_USER, None, None
    droplet_name = _env_do_str(raw, "droplet_name")
    ssh_user = _env_do_str(raw, "ssh_user") or DEFAULT_SSH_USER
    return droplet_name, ssh_user, _env_do_str(raw, "size"), _env_do_str(raw, "region")


def read_env_do_link(info: dict[str, Any]) -> EnvDoLink | None:
    """Parse ``digitalocean`` block when ``droplet_name`` is set explicitly in info.yaml."""
    droplet_name, ssh_user, size, region = _parse_env_do_block(info.get("digitalocean"))
    if not droplet_name:
        return None
    return EnvDoLink(
        droplet_name=normalize_droplet_hostname(droplet_name),
        ssh_user=ssh_user,
        size=size,
        region=region,
        explicit_droplet_name=True,
    )


def resolve_env_do_link(
    config: ProjectConfig,
    env_name: str,
    info: dict[str, Any],
) -> EnvDoLink:
    """Resolve droplet link: explicit ``digitalocean.droplet_name`` or ``{project}-{env}``."""
    droplet_name, ssh_user, size, region = _parse_env_do_block(info.get("digitalocean"))
    if droplet_name:
        return EnvDoLink(
            droplet_name=normalize_droplet_hostname(droplet_name),
            ssh_user=ssh_user,
            size=size,
            region=region,
            explicit_droplet_name=True,
        )
    return EnvDoLink(
        droplet_name=default_droplet_name(config, env_name),
        ssh_user=ssh_user,
        size=size,
        region=region,
        explicit_droplet_name=False,
    )


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


def find_droplet_for_link(
    config: ProjectConfig,
    link: EnvDoLink,
    *,
    context: str | None,
) -> dict[str, Any] | None:
    """Look up droplet by name, scoped to tooling ``digitalocean.project_*`` when set."""
    do_config = config.digitalocean
    if do_config and (do_config.project_id or do_config.project_name):
        from catalpa_tooling.doctl_projects import (
            find_project_droplet_by_name,
            resolve_project_id,
        )

        project_id = resolve_project_id(None, do_config=do_config, context=context)
        return find_project_droplet_by_name(
            project_id,
            link.droplet_name,
            context=context,
        )
    return find_droplet_by_name(link.droplet_name, context=context)


def droplet_name_to_env_map(config: ProjectConfig) -> dict[str, str]:
    """Map resolved droplet name (lower) → deploy env directory name."""
    out: dict[str, str] = {}
    for env_name in _list_env_names(config):
        info_path = config.deploy_envs_dir / env_name / "info.yaml"
        if not info_path.is_file():
            continue
        with open(info_path, encoding="utf-8") as f:
            info = yaml.safe_load(f) or {}
        if not isinstance(info, dict):
            info = {}
        link = resolve_env_do_link(config, env_name, info)
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


def _docker_host_ssh_target(docker_host: str) -> str:
    """Return ``user@host`` for comparison, or ``""`` if unparseable."""
    raw = (docker_host or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme and parsed.scheme != "ssh":
            return ""
        host = parsed.hostname or ""
        user = parsed.username or DEFAULT_SSH_USER
        return f"{user}@{host}" if host else ""
    if "@" in raw:
        return raw
    return f"{DEFAULT_SSH_USER}@{raw}" if raw else ""


def _compare_docker_host(configured: str, resolved: str, *, writing: bool = False) -> None:
    configured_target = _docker_host_ssh_target(configured)
    resolved_target = _docker_host_ssh_target(resolved)
    if not configured_target or not resolved_target:
        return
    if configured_target != resolved_target:
        if writing:
            print(
                f"Note: info.yaml docker_host ({configured!r}) differs from droplet "
                f"({resolved!r}); updating.",
                file=sys.stderr,
            )
        else:
            print(
                f"Note: info.yaml docker_host ({configured!r}) differs from droplet "
                f"({resolved!r}). Run with --write to update, or fix the droplet name.",
                file=sys.stderr,
            )


def _print_configured_docker_host(docker_host: str) -> int:
    value = (docker_host or "").strip()
    if not value:
        return 1
    print(f"docker_host: {value}")
    return 0


def _print_droplet_name_source(
    *,
    config: ProjectConfig,
    env_name: str,
    link: EnvDoLink,
) -> None:
    if link.explicit_droplet_name:
        print(f"Using droplet {link.droplet_name!r} from info.yaml.", file=sys.stderr)
    else:
        print(
            f"Using default droplet name {link.droplet_name!r} "
            f"({config.meta.name!r} + {env_name!r}; override with digitalocean.droplet_name).",
            file=sys.stderr,
        )


def print_host_resolution(
    *,
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


def _verify_droplet(link: EnvDoLink, droplet: dict[str, Any]) -> int:
    status = str(droplet.get("status", ""))
    if status != "active":
        print(
            f"Droplet {link.droplet_name!r} status is {status!r} (expected 'active').",
            file=sys.stderr,
        )
        return 1
    if not public_ipv4(droplet):
        print(
            f"Droplet {link.droplet_name!r} has no public IPv4 yet (status: {status!r}).",
            file=sys.stderr,
        )
        return 1
    return 0


def suggest_host_write_command(env_name: str) -> str:
    return f"dk {env_name} host --write"


def _print_host_provisioning_recovery(
    env_name: str,
    *,
    known_hosts_failed: bool = False,
    dns_sync_failed: bool = False,
    dns_verify_failed: bool = False,
) -> None:
    print("Droplet and docker_host are ready. To finish:", file=sys.stderr)
    if known_hosts_failed:
        print(f"  dk {env_name} host --write", file=sys.stderr)
    if dns_sync_failed:
        print(f"  dk {env_name} host --sync-dns", file=sys.stderr)
    if dns_verify_failed:
        print(f"  dk {env_name} host", file=sys.stderr)


def _finish_host_provisioning(
    config: ProjectConfig,
    env_name: str,
    *,
    context: str | None,
    dry_run: bool = False,
) -> int:
    """Patch docker_host, register SSH known_hosts, sync DO DNS, verify DNS."""
    failures: list[tuple[str, int]] = []

    host_rc = cmd_env_host(
        config,
        env_name,
        write=True,
        dry_run=dry_run,
        verify_dns=False,
        recovery_env_name=env_name,
    )
    if host_rc != 0:
        failures.append(("known_hosts", host_rc))

    loaded = load_env_info(config, env_name)
    if loaded is None:
        return 1
    _, info = loaded
    link = resolve_env_do_link(config, env_name, info)

    try:
        droplet = find_droplet_for_link(config, link, context=context)
    except SystemExit:
        return 1
    if droplet is None:
        print(
            f"No DigitalOcean droplet named {link.droplet_name!r}.",
            file=sys.stderr,
        )
        return 1

    ip = public_ipv4(droplet)
    if not ip:
        print(
            f"Droplet {link.droplet_name!r} has no public IPv4 yet.",
            file=sys.stderr,
        )
        return 1

    from catalpa_tooling.host_storage import ensure_do_block_volumes_for_specs
    from catalpa_tooling.storage_config import parse_storage_volumes_from_info

    try:
        storage_specs = parse_storage_volumes_from_info(info, config)
    except Exception as exc:
        from catalpa_tooling.storage_config import StorageConfigError

        if isinstance(exc, StorageConfigError):
            print(str(exc), file=sys.stderr)
            return 1
        raise
    if storage_specs:
        do_rc = ensure_do_block_volumes_for_specs(
            config,
            env_name,
            info,
            storage_specs,
            context=context,
            dry_run=dry_run,
        )
        if do_rc != 0:
            failures.append(("storage", do_rc))

    from catalpa_tooling.doctl_domains import sync_host_dns

    sync_rc = sync_host_dns(
        config,
        info,
        droplet_ip=ip,
        context=context,
        dry_run=dry_run,
    )
    if sync_rc != 0:
        failures.append(("dns_sync", sync_rc))

    dns_rc = _run_host_dns_checks(
        config,
        info,
        expected_ip=ip,
        context=context,
        include_do_api=True,
        env_name=env_name,
    )
    if dns_rc != 0:
        failures.append(("dns_verify", dns_rc))

    if not failures:
        return 0

    _print_host_provisioning_recovery(
        env_name,
        known_hosts_failed=any(name == "known_hosts" for name, _ in failures),
        dns_sync_failed=any(name == "dns_sync" for name, _ in failures),
        dns_verify_failed=any(name == "dns_verify" for name, _ in failures),
    )
    return max(rc for _, rc in failures)


def _print_no_doctl_hints(
    *,
    env_name: str,
    link: EnvDoLink,
    info_path: Path,
) -> None:
    print(
        f"No docker_host in {info_path} and doctl is not available.",
        file=sys.stderr,
    )
    print(
        f"Expected droplet: {link.droplet_name!r}. "
        f"Install doctl and re-run `dk {env_name} host`, or set docker_host manually.",
        file=sys.stderr,
    )
    print(f"Optional override in info.yaml:\n{_INFO_DO_EXAMPLE}", file=sys.stderr)


def _run_host_dns_checks(
    config: ProjectConfig,
    info: dict[str, Any],
    *,
    expected_ip: str,
    context: str | None,
    include_do_api: bool,
    env_name: str | None = None,
) -> int:
    """Run DO API and/or public DNS verification; aggregate failures."""
    failures: list[int] = []

    if include_do_api:
        from catalpa_tooling.doctl_domains import verify_host_dns

        rc = verify_host_dns(config, info, droplet_ip=expected_ip, context=context)
        if rc != 0:
            failures.append(rc)

    from catalpa_tooling.dns_resolve import verify_public_dns_from_info

    rc = verify_public_dns_from_info(
        info,
        expected_ip,
        recovery_env_name=env_name,
    )
    if rc != 0:
        failures.append(rc)

    return 1 if failures else 0


def _cmd_env_host_manual(
    config: ProjectConfig,
    env_name: str,
    info: dict[str, Any],
    *,
    configured_host: str,
    write: bool = False,
    verify_dns: bool = True,
) -> int:
    """Verify manual ``docker_host`` and optional public DNS (no droplet / doctl DNS API)."""
    if write:
        print(
            f"dk {env_name} host --write is not available when digitalocean.disabled is set; "
            "edit docker_host in info.yaml manually.",
            file=sys.stderr,
        )
        return 1

    if is_digitalocean_host_disabled(info):
        print(
            "DigitalOcean host integration disabled for this environment "
            "(digitalocean.disabled).",
            file=sys.stderr,
        )

    value = configured_host.strip()
    if not value:
        print(
            f"docker_host is not set; configure it in docker/envs/{env_name}/info.yaml.",
            file=sys.stderr,
        )
        return 1

    try:
        from catalpa_tooling.dns_resolve import docker_host_expected_ipv4

        expected_ip = docker_host_expected_ipv4(value)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(f"docker_host: {value}")

    if verify_dns:
        return _run_host_dns_checks(
            config,
            info,
            expected_ip=expected_ip,
            context=None,
            include_do_api=False,
            env_name=env_name,
        )
    return 0


def cmd_env_host(
    config: ProjectConfig,
    env_name: str,
    *,
    write: bool = False,
    dry_run: bool = False,
    verify_dns: bool = True,
    sync_dns: bool = False,
    recovery_env_name: str | None = None,
) -> int:
    """Resolve droplet IP for ``env_name`` and print or patch ``docker_host`` in info.yaml."""
    from catalpa_tooling.doctl_binary import (
        DoctlCommandError,
        DoctlNotFoundError,
        print_doctl_required,
        try_resolve_doctl_binary,
    )

    loaded = load_env_info(config, env_name)
    if loaded is None:
        return 1
    info_path, info = loaded
    link = resolve_env_do_link(config, env_name, info)
    configured_host = str(info.get("docker_host") or "").strip()
    do_disabled = is_digitalocean_host_disabled(info)

    if do_disabled:
        return _cmd_env_host_manual(
            config,
            env_name,
            info,
            configured_host=configured_host,
            write=write,
            verify_dns=verify_dns,
        )

    if write and not dry_run and try_resolve_doctl_binary() is None:
        print(
            f"dk {env_name} host --write requires the official doctl binary on PATH (or DOCTL_BIN).",
            file=sys.stderr,
        )
        print_doctl_required(None)
        return 1

    do_config = config.digitalocean
    context = do_config.context if do_config else None

    if try_resolve_doctl_binary() is None:
        if configured_host:
            return _cmd_env_host_manual(
                config,
                env_name,
                info,
                configured_host=configured_host,
                write=write,
                verify_dns=verify_dns,
            )
        _print_no_doctl_hints(env_name=env_name, link=link, info_path=info_path)
        return 1

    _print_droplet_name_source(config=config, env_name=env_name, link=link)

    if dry_run and write:
        print(
            f"dry-run: would look up droplet {link.droplet_name!r} and patch {info_path}",
            file=sys.stderr,
        )
        return 0

    try:
        droplet = find_droplet_for_link(config, link, context=context)
    except SystemExit:
        return 1
    except DoctlNotFoundError as e:
        if configured_host and not write:
            return _cmd_env_host_manual(
                config,
                env_name,
                info,
                configured_host=configured_host,
                write=False,
                verify_dns=verify_dns,
            )
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
            f"Create one, e.g.:\n  dk {env_name} host create",
            file=sys.stderr,
        )
        return 1

    rc = _verify_droplet(link, droplet)
    if rc != 0:
        return rc

    ip = public_ipv4(droplet)
    docker_host = format_docker_host(link.ssh_user, ip)
    print_host_resolution(link=link, droplet=droplet, docker_host=docker_host)

    if configured_host:
        _compare_docker_host(configured_host, docker_host, writing=write)

    # host --write only refreshes docker_host; DNS checks run on plain `dk <env> host`
    # and after `host create` (which syncs DO A records first).
    if sync_dns:
        from catalpa_tooling.doctl_domains import sync_host_dns

        return sync_host_dns(
            config,
            info,
            droplet_ip=ip,
            context=context,
            dry_run=dry_run,
        )

    if verify_dns and not write:
        dns_rc = _run_host_dns_checks(
            config,
            info,
            expected_ip=ip,
            context=context,
            include_do_api=True,
            env_name=env_name,
        )
        if dns_rc != 0:
            return dns_rc

    if write:
        rc = patch_info_docker_host(info_path, docker_host, dry_run=dry_run)
        if rc != 0:
            return rc
        from catalpa_tooling.ssh_known_hosts import ensure_ssh_known_host_for_docker_host

        return ensure_ssh_known_host_for_docker_host(
            docker_host,
            dry_run=dry_run,
            recovery_env_name=recovery_env_name or env_name,
        )
    return 0


def droplet_name_for_env(config: ProjectConfig, env_name: str) -> str | None:
    """Return resolved droplet name for ``env_name``, or None if info.yaml is missing."""
    loaded = load_env_info(config, env_name)
    if loaded is None:
        return None
    _, info = loaded
    return resolve_env_do_link(config, env_name, info).droplet_name


def _host_create_parser(env_name: str) -> argparse.ArgumentParser:
    from catalpa_tooling.cloud_config.render import DEFAULT_TIMEZONE

    p = argparse.ArgumentParser(
        prog=f"dk {env_name} host create",
        description=(
            "Create the DigitalOcean droplet for this environment, wait until active, "
            "then update docker_host in info.yaml."
        ),
    )
    p.add_argument(
        "--project",
        metavar="NAME|UUID",
        help="Project name or UUID (default: digitalocean.* in tooling.yaml)",
    )
    p.add_argument(
        "--size",
        help="Droplet size slug (default: info.yaml digitalocean.size, then tooling.yaml)",
    )
    p.add_argument(
        "--image",
        help="Image slug (default: ubuntu-24-04-x64 or digitalocean.image)",
    )
    p.add_argument(
        "--region",
        help="Region slug (default: info.yaml digitalocean.region, then tooling.yaml)",
    )
    p.add_argument(
        "--ssh-key",
        action="append",
        dest="ssh_keys",
        default=[],
        metavar="ID|FINGERPRINT",
        help="SSH key ID or fingerprint (repeatable; default: all keys from host doctl)",
    )
    p.add_argument(
        "--timezone",
        help=f"IANA timezone (default: {DEFAULT_TIMEZONE} or digitalocean.timezone)",
    )
    p.add_argument("--context", help="Authentication context")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print cloud-config and host doctl command without creating or patching",
    )
    p.add_argument(
        "--no-monitoring",
        action="store_true",
        help="Omit --enable-monitoring (default: install DO metrics agent)",
    )
    p.add_argument(
        "--no-reuse-existing",
        action="store_true",
        help="Fail if the env droplet already exists instead of finishing provisioning",
    )
    return p


def cmd_env_host_create(
    config: ProjectConfig,
    env_name: str,
    argv: list[str],
    *,
    global_dry_run: bool = False,
    deprecation_message: str | None = None,
) -> int:
    """Create env droplet (always wait), then patch ``docker_host`` in info.yaml."""
    from catalpa_tooling.doctl_binary import ensure_doctl_available
    from catalpa_tooling.doctl_droplets import create_droplet
    from catalpa_tooling.doctl_projects import (
        resolve_project_id,
        resolve_project_id_dry_run,
    )

    if deprecation_message:
        print(deprecation_message, file=sys.stderr)

    if "--wait" in argv or "--no-wait" in argv:
        print(
            f"dk {env_name} host create always waits for the droplet to become active; "
            "do not pass --wait or --no-wait.",
            file=sys.stderr,
        )
        return 1

    p = _host_create_parser(env_name)
    ns, rest = p.parse_known_args(argv)
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")

    dry_run = global_dry_run or ns.dry_run

    loaded = load_env_info(config, env_name)
    if loaded is None:
        return 1
    _, info = loaded

    if is_digitalocean_host_disabled(info):
        print(
            f"dk {env_name} host create is not available when digitalocean.disabled is set "
            "in info.yaml. Remove the flag or set disabled: false to provision on DigitalOcean.",
            file=sys.stderr,
        )
        return 1

    link = resolve_env_do_link(config, env_name, info)
    _print_droplet_name_source(config=config, env_name=env_name, link=link)

    do_config = config.digitalocean
    context = ns.context or (do_config.context if do_config else None)

    if dry_run:
        project_id = resolve_project_id_dry_run(ns.project, do_config=do_config)
    else:
        ensure_doctl_available()
        try:
            project_id = resolve_project_id(ns.project, do_config=do_config, context=context)
        except SystemExit:
            return 1

    rc = create_droplet(
        link.droplet_name,
        size=ns.size,
        image=ns.image,
        region=ns.region,
        env_size=link.size,
        env_region=link.region,
        project_id=project_id,
        ssh_keys=tuple(ns.ssh_keys),
        timezone=ns.timezone,
        context=context,
        wait=True,
        dry_run=dry_run,
        do_config=do_config,
        for_env=None,
        enable_monitoring=False if ns.no_monitoring else None,
        reuse_existing=not ns.no_reuse_existing,
    )
    if rc != 0 or dry_run:
        return rc
    return _finish_host_provisioning(
        config,
        env_name,
        context=context,
        dry_run=False,
    )
