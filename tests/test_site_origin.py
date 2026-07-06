"""Tests for site_origin / domain parsing and compose env injection."""

from __future__ import annotations

import catalpa_tooling.site_origin as so
import pytest

from catalpa_tooling.managed_deploy_env import load_managed_deploy_context
from catalpa_tooling.site_origin import (
    derive_dev_hostname,
    derive_site_origin,
    domain_env_from_origins,
    hostnames_from_origins,
    normalize_site_origin_entry,
    parse_site_origin_entries,
    parse_site_origins_from_info,
    primary_site_origin_for_env,
    primary_site_origin_from_info,
    project_slug_from_config,
    resolve_site_origins_for_env,
)


def test_normalize_hostname() -> None:
    assert normalize_site_origin_entry("catalpa.io") == "https://catalpa.io"


def test_normalize_url() -> None:
    assert normalize_site_origin_entry("https://www.example.com/path") == "https://www.example.com"


def test_parse_list_hostnames() -> None:
    origins = parse_site_origin_entries(
        ["catalpa.io", "www.catalpa.io"],
        field="site_origin",
    )
    assert origins == ["https://catalpa.io", "https://www.catalpa.io"]
    assert domain_env_from_origins(origins) == "catalpa.io, www.catalpa.io"


def test_parse_mixed_list() -> None:
    origins = parse_site_origin_entries(
        ["catalpa.io", "https://getbero.io"],
        field="site_origin",
    )
    assert hostnames_from_origins(origins) == ["catalpa.io", "getbero.io"]


def test_parse_single_string() -> None:
    assert parse_site_origin_entries("https://staging.example.com", field="site_origin") == [
        "https://staging.example.com"
    ]


def test_legacy_domain_warning(capsys: pytest.CaptureFixture[str]) -> None:
    so._LEGACY_DOMAIN_WARNED = False
    origins = parse_site_origins_from_info({"domain": ["legacy.example.com"]})
    assert origins == ["https://legacy.example.com"]
    err = capsys.readouterr().err
    assert "deprecated" in err


def test_site_origin_precedence_over_domain() -> None:
    so._LEGACY_DOMAIN_WARNED = False
    info = {
        "site_origin": ["primary.example.com"],
        "domain": ["ignored.example.com"],
    }
    assert parse_site_origins_from_info(info) == ["https://primary.example.com"]


def test_env_nested_fallback() -> None:
    info = {"env": {"site_origin": ["nested.example.com"]}}
    assert primary_site_origin_from_info(info) == "https://nested.example.com"


def test_invalid_type_raises() -> None:
    with pytest.raises(ValueError, match="site_origin"):
        parse_site_origin_entries(42, field="site_origin")


def test_load_managed_deploy_context_sets_domain_and_site_origin(
    minimal_project,
    tmp_path: pytest.TempPathFactory,
) -> None:
    env_dir = minimal_project.deploy_envs_dir / "local"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "info.yaml").write_text(
        "name: local\n"
        "site_origin:\n"
        "  - web.example.com\n"
        "  - https://api.example.com\n"
        "credentials_decrypt_optional: true\n",
        encoding="utf-8",
    )
    ctx = load_managed_deploy_context(minimal_project, "local")
    assert ctx is not None
    assert ctx.site_origin == "https://web.example.com"
    assert ctx.site_origins == (
        "https://web.example.com",
        "https://api.example.com",
    )
    assert ctx.env_add["SITE_ORIGIN"] == "https://web.example.com"
    assert ctx.env_add["DOMAIN"] == "web.example.com, api.example.com"


def test_load_managed_deploy_context_respects_django_debug_from_info(
    minimal_project,
    tmp_path: pytest.TempPathFactory,
) -> None:
    env_dir = minimal_project.deploy_envs_dir / "local"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "info.yaml").write_text(
        "name: local\n"
        "site_origin: http://dev.example\n"
        "credentials_decrypt_optional: true\n"
        "env:\n"
        "  django_debug: 'true'\n",
        encoding="utf-8",
    )
    ctx = load_managed_deploy_context(minimal_project, "local")
    assert ctx is not None
    assert ctx.env_add["DJANGO_DEBUG"] == "true"


def test_load_managed_deploy_context_defaults_django_debug_off(
    minimal_project,
    tmp_path: pytest.TempPathFactory,
) -> None:
    env_dir = minimal_project.deploy_envs_dir / "local"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "info.yaml").write_text(
        "name: local\n"
        "site_origin: https://staging.example\n"
        "credentials_decrypt_optional: true\n",
        encoding="utf-8",
    )
    ctx = load_managed_deploy_context(minimal_project, "local")
    assert ctx is not None
    assert ctx.env_add["DJANGO_DEBUG"] == "0"


def test_derive_dev_hostname(minimal_config) -> None:
    assert derive_dev_hostname(minimal_config, "dev") == "minimal-dev.localdev.temp.build"
    assert (
        derive_dev_hostname(minimal_config, "full", role="stats")
        == "stats.minimal-full.localdev.temp.build"
    )
    assert project_slug_from_config(minimal_config) == "minimal"
    assert derive_site_origin(minimal_config, "dev") == "https://minimal-dev.localdev.temp.build"


def test_resolve_site_origins_for_env(minimal_config) -> None:
    info = {"local_proxy": {"roles": ["admin", "stats"]}}
    origins = resolve_site_origins_for_env(info, minimal_config, "full")
    assert origins == [
        "https://minimal-full.localdev.temp.build",
        "https://admin.minimal-full.localdev.temp.build",
        "https://stats.minimal-full.localdev.temp.build",
    ]


def test_primary_site_origin_for_env_derives_when_missing(minimal_config) -> None:
    assert primary_site_origin_for_env({}, minimal_config, "dev") == (
        "https://minimal-dev.localdev.temp.build"
    )
