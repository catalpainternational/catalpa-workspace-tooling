"""Parse ``site_origin`` / legacy ``domain`` from ``docker/envs/<env>/info.yaml``."""

from __future__ import annotations

import sys
from urllib.parse import urlparse

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


def primary_site_origin_from_info(info: dict) -> str:
    """First normalized origin, or empty string."""
    origins = parse_site_origins_from_info(info)
    return origins[0] if origins else ""


def site_origin_from_info(info: dict) -> str:
    """Primary public site origin URL (backward-compatible alias)."""
    return primary_site_origin_from_info(info)
