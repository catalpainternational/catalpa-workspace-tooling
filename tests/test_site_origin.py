"""Tests for site_origin / domain parsing and compose env injection."""

from __future__ import annotations

import catalpa_tooling.site_origin as so
import pytest

from catalpa_tooling.managed_deploy_env import load_managed_deploy_context
from catalpa_tooling.site_origin import (
    domain_env_from_origins,
    hostnames_from_origins,
    normalize_site_origin_entry,
    parse_site_origin_entries,
    parse_site_origins_from_info,
    primary_site_origin_from_info,
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
