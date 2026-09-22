"""Tests for DJANGO_ORIGIN / DJANGO_EXTRA_ORIGINS host handling."""

from __future__ import annotations

from types import SimpleNamespace

from catalpa_tooling.caddy_site_addresses import apply_caddy_site_addresses
from catalpa_tooling.local_proxy import local_proxy_routes
from catalpa_tooling.site_origin import (
    django_site_hosts_from_env,
    format_caddy_site_hosts,
    split_env_host_tokens,
)


def test_split_env_host_tokens_accepts_commas_and_spaces() -> None:
    assert split_env_host_tokens("localhost, 127.0.0.1 other.local") == [
        "localhost",
        "127.0.0.1",
        "other.local",
    ]
    assert split_env_host_tokens(None) == []


def test_django_site_hosts_from_origins() -> None:
    hosts = django_site_hosts_from_env(
        {
            "DJANGO_ORIGIN": "https://app.example:8443",
            "DJANGO_EXTRA_ORIGINS": (
                "https://one.example, two.example https://app.example:8443"
            ),
        }
    )
    assert hosts == ["app.example:8443", "one.example", "two.example"]
    assert format_caddy_site_hosts(hosts) == (
        "app.example:8443 one.example two.example"
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        paths=SimpleNamespace(frontend="."),
        has_metabase_fetch=lambda: False,
        meta=SimpleNamespace(name="generic"),
        dev=SimpleNamespace(site_origin_base="localdev.temp.build"),
        stack_service=lambda role: {
            "proxy": "caddy",
            "web": "django",
            "db": "db",
        }[role],
        image_component=lambda _: "generic",
    )


def test_caddy_django_hosts_include_explicit_extras() -> None:
    env = {
        "DJANGO_ORIGIN": "https://app.localhost",
        "DJANGO_EXTRA_ORIGINS": "https://one.localhost, two.localhost",
    }
    apply_caddy_site_addresses(
        env,
        info={},
        config=_config(),  # type: ignore[arg-type]
        env_name="dev",
        site_origin="https://app.localhost",
        site_origins=["https://app.localhost"],
        behind_local_proxy=True,
    )
    assert env["CADDY_DJANGO_SITE_HOSTS"] == (
        "app.localhost one.localhost two.localhost"
    )


def test_local_proxy_routes_include_django_extra_hosts() -> None:
    info = {
        "env": {
            "DJANGO_ORIGIN": "https://app.localhost",
            "DJANGO_EXTRA_ORIGINS": "https://one.localhost two.localhost",
        },
        "local_proxy": {},
    }
    routes = local_proxy_routes(
        info,
        _config(),  # type: ignore[arg-type]
        "dev",
        "generic_dev",
    )
    assert {route.host for route in routes} == {
        "generic-dev.localdev.temp.build",
        "app.localhost",
        "one.localhost",
        "two.localhost",
    }
