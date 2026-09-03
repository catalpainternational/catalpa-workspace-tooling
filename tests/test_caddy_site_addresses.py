"""Tests for CADDY_*_SITE_ADDRESS injection (deployed HTTPS + local-proxy HTTP)."""

from __future__ import annotations

from types import SimpleNamespace

from catalpa_tooling.caddy_site_addresses import apply_caddy_site_addresses, is_bero_stack


def _config(frontend: str = "frontend", *, metabase: bool = False) -> object:
    """Lightweight stand-in exposing the attrs the helper reads.

    Deployed tests always pass an explicit ``site_origin`` so the helper never needs the
    dev-hostname derivation (which requires a full ``ProjectConfig``).
    """
    return SimpleNamespace(
        paths=SimpleNamespace(frontend=frontend),
        has_metabase_fetch=lambda: metabase,
    )


def _deployed(
    env_add: dict[str, str],
    config: object,
    *,
    info: dict | None = None,
    site_origin: str = "https://app.example.com",
    site_origins: list[str] | None = None,
) -> dict[str, str]:
    apply_caddy_site_addresses(
        env_add,
        info=info or {},
        config=config,  # type: ignore[arg-type]
        env_name="staging",
        site_origin=site_origin,
        site_origins=site_origins or [site_origin],
        behind_local_proxy=False,
    )
    return env_add


def test_is_bero_stack() -> None:
    assert is_bero_stack(_config("bero"))  # type: ignore[arg-type]
    assert not is_bero_stack(_config("frontend"))  # type: ignore[arg-type]


def test_deployed_generic_sets_only_primary() -> None:
    env = _deployed({}, _config("frontend"))
    assert env["CADDY_SITE_ADDRESS"] == "https://app.example.com"
    assert "CADDY_DJANGO_SITE_ADDRESS" not in env
    assert "CADDY_METABASE_SITE_ADDRESS" not in env
    assert "DJANGO_ORIGIN" not in env


def test_deployed_bero_with_explicit_origins() -> None:
    env = _deployed(
        {
            "DJANGO_ORIGIN": "https://admin.samtuku.temp.build",
            "METABASE_ORIGIN": "https://stats.samtuku.temp.build",
        },
        _config("bero", metabase=True),
        site_origin="https://samtuku.temp.build",
    )
    assert env["CADDY_SITE_ADDRESS"] == "https://samtuku.temp.build"
    assert env["CADDY_DJANGO_SITE_ADDRESS"] == "https://admin.samtuku.temp.build"
    assert env["CADDY_METABASE_SITE_ADDRESS"] == "https://stats.samtuku.temp.build"


def test_deployed_bero_staging_style_derives_admin_and_stats() -> None:
    env = _deployed(
        {},
        _config("bero", metabase=True),
        site_origin="https://samtuku.temp.build",
    )
    assert env["CADDY_SITE_ADDRESS"] == "https://samtuku.temp.build"
    assert env["DJANGO_ORIGIN"] == "https://admin.samtuku.temp.build"
    assert env["CADDY_DJANGO_SITE_ADDRESS"] == "https://admin.samtuku.temp.build"
    assert env["CADDY_METABASE_SITE_ADDRESS"] == "https://stats.samtuku.temp.build"


def test_deployed_bero_without_metabase_fetch_skips_metabase() -> None:
    env = _deployed(
        {},
        _config("bero", metabase=False),
        site_origin="https://samtuku.temp.build",
    )
    assert env["CADDY_DJANGO_SITE_ADDRESS"] == "https://admin.samtuku.temp.build"
    assert "CADDY_METABASE_SITE_ADDRESS" not in env


def test_deployed_ambulancia_style_metabase_site_origin() -> None:
    env = _deployed(
        {"METABASE_SITE_ORIGIN": "https://metabase.ambulancia-staging.catalpa.build"},
        _config("."),
        site_origin="https://ambulancia-staging.catalpa.build",
        site_origins=[
            "https://ambulancia-staging.catalpa.build",
            "https://metabase.ambulancia-staging.catalpa.build",
        ],
    )
    assert env["CADDY_METABASE_SITE_ADDRESS"] == (
        "https://metabase.ambulancia-staging.catalpa.build"
    )
    assert "CADDY_DJANGO_SITE_ADDRESS" not in env
    assert "DJANGO_ORIGIN" not in env


def test_deployed_non_bero_second_site_origin_fallback() -> None:
    env = _deployed(
        {},
        _config("."),
        site_origin="https://primary.example.com",
        site_origins=["https://primary.example.com", "https://metabase.example.com"],
    )
    assert env["CADDY_METABASE_SITE_ADDRESS"] == "https://metabase.example.com"


def test_deployed_non_metabase_project_sets_no_metabase_address() -> None:
    env = _deployed({}, _config("."))
    assert "CADDY_METABASE_SITE_ADDRESS" not in env


def test_deployed_respects_explicit_override() -> None:
    env = _deployed(
        {"CADDY_SITE_ADDRESS": "https://custom.example.com"},
        _config("bero", metabase=True),
        site_origin="https://samtuku.temp.build",
    )
    assert env["CADDY_SITE_ADDRESS"] == "https://custom.example.com"


def test_deployed_stats_role_sets_metabase_for_non_bero() -> None:
    env = _deployed(
        {},
        _config("."),
        info={"local_proxy": {"roles": ["stats"]}},
        site_origin="https://app.example.com",
    )
    assert env["CADDY_METABASE_SITE_ADDRESS"] == "https://stats.app.example.com"


def test_local_proxy_uses_http_addresses() -> None:
    env: dict[str, str] = {}
    apply_caddy_site_addresses(
        env,
        info={"local_proxy": {"roles": ["admin", "stats"]}},
        config=_config("bero", metabase=True),  # type: ignore[arg-type]
        env_name="dev",
        site_origin="https://ncd-dev.localdev.temp.build",
        site_origins=["https://ncd-dev.localdev.temp.build"],
        behind_local_proxy=True,
    )
    assert env["CADDY_SITE_ADDRESS"] == "http://ncd-dev.localdev.temp.build"
    assert env["CADDY_DJANGO_SITE_ADDRESS"] == "http://admin.ncd-dev.localdev.temp.build"
    assert env["CADDY_METABASE_SITE_ADDRESS"] == "http://stats.ncd-dev.localdev.temp.build"
    assert env["DJANGO_ORIGIN"] == "https://admin.ncd-dev.localdev.temp.build"
    assert env["METABASE_ORIGIN"] == "https://stats.ncd-dev.localdev.temp.build"
    assert env["METABASE_SITE_ORIGIN"] == "https://stats.ncd-dev.localdev.temp.build"
    assert env["VITE_BEHIND_PROXY"] == "true"
    allowed = env["BERO_EXTRA_ALLOWED_HOSTS"]
    assert "admin.ncd-dev.localdev.temp.build" in allowed
    assert "stats.ncd-dev.localdev.temp.build" in allowed
    django_extra = env["DJANGO_EXTRA_ORIGINS"]
    assert "admin.ncd-dev.localdev.temp.build" in django_extra
    assert "stats.ncd-dev.localdev.temp.build" in django_extra
    assert env["CADDY_DJANGO_SITE_HOSTS"] == (
        "admin.ncd-dev.localdev.temp.build stats.ncd-dev.localdev.temp.build"
    )


def test_deployed_redirect_origins_injects_caddy_addresses() -> None:
    env = _deployed(
        {},
        _config("bero", metabase=True),
        info={
            "redirect_origins": [
                "www.example.org",
                "https://example.com",
            ],
        },
        site_origin="https://example.org",
    )
    assert env["CADDY_SITE_ADDRESS"] == "https://example.org"
    assert env["CADDY_REDIRECT_SITE_ADDRESSES"] == (
        "https://www.example.org https://example.com"
    )


def test_deployed_no_redirect_origins_skips_caddy_redirect() -> None:
    env = _deployed({}, _config("frontend"))
    assert "CADDY_REDIRECT_SITE_ADDRESSES" not in env


def test_deployed_redirect_origins_respect_explicit_override() -> None:
    env = _deployed(
        {"CADDY_REDIRECT_SITE_ADDRESSES": "https://custom.example.com"},
        _config("bero"),
        info={"redirect_origins": ["www.example.org"]},
        site_origin="https://example.org",
    )
    assert env["CADDY_REDIRECT_SITE_ADDRESSES"] == "https://custom.example.com"


def test_local_proxy_redirect_origins_use_http() -> None:
    env: dict[str, str] = {}
    apply_caddy_site_addresses(
        env,
        info={
            "local_proxy": {"roles": ["admin"]},
            "redirect_origins": ["www.example.org", "alias.example.com"],
        },
        config=_config("bero"),  # type: ignore[arg-type]
        env_name="dev",
        site_origin="https://app-dev.localdev.temp.build",
        site_origins=["https://app-dev.localdev.temp.build"],
        behind_local_proxy=True,
    )
    assert env["CADDY_REDIRECT_SITE_ADDRESSES"] == (
        "http://www.example.org http://alias.example.com"
    )
    assert "BERO_EXTRA_ALLOWED_HOSTS" in env
    assert "www.example.org" not in env["BERO_EXTRA_ALLOWED_HOSTS"]
    assert "alias.example.com" not in env["BERO_EXTRA_ALLOWED_HOSTS"]
