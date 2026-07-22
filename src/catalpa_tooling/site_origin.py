"""Parse ``site_origin`` / legacy ``domain`` from ``docker/envs/<env>/info.yaml``."""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from catalpa_tooling.config import ProjectConfig

_LEGACY_DOMAIN_WARNED = False


def _warn_legacy_domain() -> None:
    global _LEGACY_DOMAIN_WARNED
    if _LEGACY_DOMAIN_WARNED:
        return
    _LEGACY_DOMAIN_WARNED = True
    print(
        "Note: info.yaml `domain` is deprecated; use `site_origin` (string or list of hostnames/URLs).",
        file=sys.stderr,
    )


def normalize_site_origin_entry(raw: str) -> str:
    """Normalize one hostname or origin URL to ``scheme://netloc`` (no path)."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty site_origin entry")
    if "://" not in s:
        return f"https://{s}"
    parsed = urlparse(s)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path.split("/")[0]
    if not netloc:
        raise ValueError(f"invalid site_origin entry: {raw!r}")
    return f"{scheme}://{netloc}"


def parse_site_origin_entries(value: object, *, field: str) -> list[str]:
    """Parse a string or YAML list into normalized origin URLs."""
    if value is None or value is False:
        return []
    if isinstance(value, str):
        s = value.strip()
        return [normalize_site_origin_entry(s)] if s else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            s = str(item).strip()
            if not s:
                continue
            out.append(normalize_site_origin_entry(s))
        return out
    raise ValueError(
        f"{field} must be a string or YAML list of hostnames/URLs "
        f"(got {type(value).__name__})"
    )


def hostnames_from_origins(origins: list[str]) -> list[str]:
    """Extract hostnames (with port if present) from normalized origins."""
    hosts: list[str] = []
    for origin in origins:
        parsed = urlparse(origin)
        host = parsed.netloc or parsed.path.split("/")[0]
        if host:
            hosts.append(host)
    return hosts


def domain_env_from_origins(origins: list[str]) -> str:
    """Comma+space separated hostnames for compose ``DOMAIN`` (Caddy + Django)."""
    return ", ".join(hostnames_from_origins(origins))


def _origins_from_field(info: dict, key: str, *, legacy_domain: bool) -> list[str]:
    if key not in info:
        return []
    value = info.get(key)
    if value is None or value is False:
        return []
    if legacy_domain and (isinstance(value, str) and value.strip() or isinstance(value, list) and value):
        _warn_legacy_domain()
    return parse_site_origin_entries(value, field=key)


def parse_site_origins_from_info(info: dict) -> list[str]:
    """Return normalized origins from ``info.yaml`` (top-level ``site_origin`` preferred)."""
    origins = _origins_from_field(info, "site_origin", legacy_domain=False)
    if origins:
        return origins
    origins = _origins_from_field(info, "domain", legacy_domain=True)
    if origins:
        return origins
    env_block = info.get("env")
    if isinstance(env_block, dict):
        origins = _origins_from_field(env_block, "site_origin", legacy_domain=False)
        if origins:
            return origins
        origins = _origins_from_field(env_block, "domain", legacy_domain=True)
        if origins:
            return origins
    return []


def parse_redirect_origins_from_info(info: dict) -> list[str]:
    """Return normalized redirect-only origins from ``info.yaml``.

    ``redirect_origins`` lists hosts that should terminate TLS and permanently redirect
    to the primary ``site_origin`` / ``BERO_ORIGIN``. They must not be listed under
    ``site_origin`` (those hosts serve the app). Nested ``env.redirect_origins`` is used
    only when the top-level field is empty.
    """
    origins = _origins_from_field(info, "redirect_origins", legacy_domain=False)
    if origins:
        return origins
    env_block = info.get("env")
    if isinstance(env_block, dict):
        return _origins_from_field(env_block, "redirect_origins", legacy_domain=False)
    return []


def dns_hostnames_from_info(info: dict) -> list[str]:
    """Hostnames for DNS verify/sync: ``site_origin`` then ``redirect_origins`` (unique)."""
    seen: set[str] = set()
    hosts: list[str] = []
    for origin in parse_site_origins_from_info(info) + parse_redirect_origins_from_info(
        info
    ):
        for host in hostnames_from_origins([origin]):
            if host not in seen:
                seen.add(host)
                hosts.append(host)
    return hosts


def primary_site_origin_from_info(info: dict) -> str:
    """First normalized origin, or empty string."""
    origins = parse_site_origins_from_info(info)
    return origins[0] if origins else ""


def site_origin_from_info(info: dict) -> str:
    """Primary public site origin URL (backward-compatible alias)."""
    return primary_site_origin_from_info(info)


def project_slug_from_config(config: ProjectConfig) -> str:
    """DNS-safe project slug from ``tooling.yaml`` ``project.name``."""
    raw = (config.meta.name or "").strip().lower().replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    if not slug:
        raise ValueError(f"invalid project.name for hostname slug: {config.meta.name!r}")
    return slug


def derive_dev_hostname(
    config: ProjectConfig,
    env_name: str,
    *,
    role: str | None = None,
) -> str:
    """Canonical local dev hostname: ``{slug}-{env}.{base}`` or ``{role}.{slug}-{env}.{base}``."""
    slug = project_slug_from_config(config)
    env = env_name.strip().lower()
    base = config.dev.site_origin_base.strip().rstrip(".")
    host = f"{slug}-{env}.{base}"
    if role:
        role_label = role.strip().lower()
        if role_label:
            host = f"{role_label}.{host}"
    return host


def derive_site_origin(
    config: ProjectConfig,
    env_name: str,
    *,
    role: str | None = None,
) -> str:
    """HTTPS origin URL for a derived local dev hostname."""
    return normalize_site_origin_entry(derive_dev_hostname(config, env_name, role=role))


def local_proxy_role_names(info: dict) -> tuple[str, ...]:
    """Role subdomain labels from ``local_proxy.roles`` (e.g. admin, stats)."""
    block = info.get("local_proxy")
    if not isinstance(block, dict):
        return ()
    raw = block.get("roles")
    if not isinstance(raw, list):
        return ()
    roles: list[str] = []
    for item in raw:
        label = str(item).strip().lower()
        if label and label not in roles:
            roles.append(label)
    return tuple(roles)


def resolve_site_origins_for_env(
    info: dict,
    config: ProjectConfig,
    env_name: str,
) -> list[str]:
    """Explicit ``site_origin`` plus derived role origins for local proxy routing."""
    explicit = parse_site_origins_from_info(info)
    if explicit:
        origins = list(explicit)
        primary = explicit[0]
    else:
        primary = derive_site_origin(config, env_name)
        origins = [primary]
    seen_hosts = set(hostnames_from_origins(origins))
    for role in local_proxy_role_names(info):
        if explicit:
            origin = role_site_origin_from_primary(primary, role)
        else:
            origin = derive_site_origin(config, env_name, role=role)
        host = hostnames_from_origins([origin])[0]
        if host not in seen_hosts:
            origins.append(origin)
            seen_hosts.add(host)
    return origins


def role_site_origin_from_primary(primary_origin: str, role: str) -> str:
    """Build ``https://{role}.{primary-host}`` from an explicit primary origin."""
    host = hostnames_from_origins([primary_origin])[0]
    role_label = role.strip().lower()
    if not role_label:
        raise ValueError("role must not be empty")
    return normalize_site_origin_entry(f"{role_label}.{host}")


def primary_site_origin_for_env(
    info: dict,
    config: ProjectConfig | None = None,
    env_name: str = "",
) -> str:
    """Primary origin: explicit ``site_origin`` or derived ``{slug}-{env}.{base}``."""
    explicit = primary_site_origin_from_info(info)
    if explicit:
        return explicit
    if config is not None and env_name:
        return derive_site_origin(config, env_name)
    return ""
