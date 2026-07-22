"""Inject Caddy site-block address env vars (``CADDY_*_SITE_ADDRESS``).

Caddy site blocks in bero (and ambulancia) stacks key off ``CADDY_SITE_ADDRESS`` /
``CADDY_DJANGO_SITE_ADDRESS`` / ``CADDY_METABASE_SITE_ADDRESS`` rather than the plain
origin vars. Those compose defaults are ``http://…`` so, without injection, a deployed
stack never turns on Caddy automatic HTTPS.

Optional ``redirect_origins`` in ``info.yaml`` become ``CADDY_REDIRECT_SITE_ADDRESSES``
(space-separated) for a redirect-only Caddy site block — never mixed into ``DOMAIN`` or
the primary app site address.

Two callers:

* **Local proxy** (``behind_local_proxy=True``) — dev/full stacks behind the machine-wide
  proxy that terminates TLS; stack Caddy stays HTTP-only, so addresses are ``http://host``.
* **Deployed** (``behind_local_proxy=False``) — remote staging/prod on ``ssh://`` hosts;
  Caddy provisions its own certs, so addresses are the ``https://`` origins.

All writes use ``setdefault`` so explicit ``info.yaml`` ``env:`` / credential values win.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from catalpa_tooling.site_origin import (
    derive_site_origin,
    hostnames_from_origins,
    local_proxy_role_names,
    parse_redirect_origins_from_info,
    role_site_origin_from_primary,
)

if TYPE_CHECKING:
    from catalpa_tooling.config import ProjectConfig


def is_bero_stack(config: ProjectConfig) -> bool:
    """True when this project embeds bero (``paths.frontend == 'bero'``).

    Only bero stacks ship the ``{$CADDY_DJANGO_SITE_ADDRESS}`` admin-redirect site block,
    so the Django Caddy address is bero-only.
    """
    return config.paths.frontend.strip() == "bero"


def _role_origin(
    config: ProjectConfig,
    env_name: str,
    site_origin: str,
    role: str,
) -> str:
    """Origin for a role subdomain: from explicit primary, else derived dev hostname."""
    if site_origin:
        return role_site_origin_from_primary(site_origin, role)
    return derive_site_origin(config, env_name, role=role)


def _apply_local_proxy(
    env_add: dict[str, str],
    *,
    info: dict,
    config: ProjectConfig,
    env_name: str,
    site_origin: str,
) -> None:
    """HTTP Caddy addresses for stacks behind the machine-wide dev proxy."""
    primary_host = hostnames_from_origins([site_origin])[0] if site_origin else ""
    if primary_host:
        env_add.setdefault("CADDY_SITE_ADDRESS", f"http://{primary_host}")

    role_env_keys: dict[str, tuple[tuple[str, str], ...]] = {
        "admin": (("DJANGO_ORIGIN", "CADDY_DJANGO_SITE_ADDRESS"),),
        "stats": (("METABASE_ORIGIN", "CADDY_METABASE_SITE_ADDRESS"),),
    }
    roles = local_proxy_role_names(info)
    extra_allowed: list[str] = []
    for role in roles:
        role_origin = _role_origin(config, env_name, site_origin, role)
        role_host = hostnames_from_origins([role_origin])[0]
        extra_allowed.append(role_host)
        for env_key, caddy_key in role_env_keys.get(role, ()):
            env_add.setdefault(env_key, role_origin)
            env_add.setdefault(caddy_key, f"http://{role_host}")
    if extra_allowed:
        env_add.setdefault("BERO_EXTRA_ALLOWED_HOSTS", ", ".join(extra_allowed))
    env_add.setdefault("VITE_BEHIND_PROXY", "true")
    if "stats" in roles:
        env_add.setdefault(
            "METABASE_SITE_ORIGIN", _role_origin(config, env_name, site_origin, "stats")
        )


def _apply_deployed(
    env_add: dict[str, str],
    *,
    info: dict,
    config: ProjectConfig,
    env_name: str,
    site_origin: str,
    site_origins: list[str],
) -> None:
    """HTTPS Caddy addresses for remote staging/prod (Caddy terminates TLS itself)."""
    if not site_origin:
        return

    env_add.setdefault("CADDY_SITE_ADDRESS", site_origin)

    if is_bero_stack(config):
        django_origin = env_add.get("DJANGO_ORIGIN")
        if not django_origin:
            django_origin = _role_origin(config, env_name, site_origin, "admin")
            env_add.setdefault("DJANGO_ORIGIN", django_origin)
        env_add.setdefault("CADDY_DJANGO_SITE_ADDRESS", django_origin)

    metabase_origin = _deployed_metabase_origin(
        env_add,
        info=info,
        config=config,
        env_name=env_name,
        site_origin=site_origin,
        site_origins=site_origins,
    )
    if metabase_origin:
        env_add.setdefault("CADDY_METABASE_SITE_ADDRESS", metabase_origin)


def _deployed_metabase_origin(
    env_add: dict[str, str],
    *,
    info: dict,
    config: ProjectConfig,
    env_name: str,
    site_origin: str,
    site_origins: list[str],
) -> str | None:
    """Metabase site-block origin for a deployed stack, or None when Metabase isn't routed.

    First matching signal wins; only explicit config / clear stack signals set the address
    so non-Metabase projects never get a bogus ``CADDY_METABASE_SITE_ADDRESS``.
    """
    explicit = env_add.get("METABASE_ORIGIN") or env_add.get("METABASE_SITE_ORIGIN")
    if explicit:
        return explicit
    if "stats" in local_proxy_role_names(info):
        return _role_origin(config, env_name, site_origin, "stats")
    if is_bero_stack(config) and config.has_metabase_fetch():
        return _role_origin(config, env_name, site_origin, "stats")
    if not is_bero_stack(config) and len(site_origins) > 1:
        return site_origins[1]
    return None


def _apply_redirect_site_addresses(
    env_add: dict[str, str],
    *,
    info: dict,
    behind_local_proxy: bool,
) -> None:
    """Inject ``CADDY_REDIRECT_SITE_ADDRESSES`` from ``redirect_origins`` (space-separated).

    Redirect hosts get TLS (when deployed) and a Caddy ``redir`` site block; they are not
    added to ``DOMAIN`` / ``BERO_EXTRA_ALLOWED_HOSTS`` / ``CADDY_SITE_ADDRESS``.
    """
    redirects = parse_redirect_origins_from_info(info)
    if not redirects:
        return
    if behind_local_proxy:
        value = " ".join(
            f"http://{host}" for host in hostnames_from_origins(redirects)
        )
    else:
        value = " ".join(redirects)
    env_add.setdefault("CADDY_REDIRECT_SITE_ADDRESSES", value)


def apply_caddy_site_addresses(
    env_add: dict[str, str],
    *,
    info: dict,
    config: ProjectConfig,
    env_name: str,
    site_origin: str,
    site_origins: list[str],
    behind_local_proxy: bool,
) -> None:
    """Populate ``CADDY_*_SITE_ADDRESS`` (and related origins) in ``env_add`` in place."""
    if behind_local_proxy:
        _apply_local_proxy(
            env_add,
            info=info,
            config=config,
            env_name=env_name,
            site_origin=site_origin,
        )
    else:
        _apply_deployed(
            env_add,
            info=info,
            config=config,
            env_name=env_name,
            site_origin=site_origin,
            site_origins=site_origins,
        )
    _apply_redirect_site_addresses(
        env_add,
        info=info,
        behind_local_proxy=behind_local_proxy,
    )
