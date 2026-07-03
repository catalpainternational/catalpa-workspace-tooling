"""Tests for catalpa_tooling.local_proxy."""

from __future__ import annotations

import pytest

from catalpa_tooling import local_proxy
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.local_proxy import (
    LOCAL_PROXY_CA_COMMON_NAME,
    LocalProxyConfigError,
    build_route_config,
    local_proxy_enabled,
    local_proxy_hostname,
    local_proxy_upstream_dial,
    route_id,
    upsert_route,
)
from catalpa_tooling.local_proxy_assets import local_proxy_caddyfile_path


def test_local_proxy_caddyfile_shipped() -> None:
    path = local_proxy_caddyfile_path()
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "local_certs" in text
    # Admin must bind to all interfaces inside the container so the loopback-only
    # published port (-p 127.0.0.1:2019:2019) can reach it.
    assert "admin 0.0.0.0:2019" in text
    # Named CA so the persisted root is recognizable in the OS trust store.
    assert "pki" in text
    assert f'root_cn "{LOCAL_PROXY_CA_COMMON_NAME}"' in text


def test_local_proxy_data_dir_respects_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert local_proxy.local_proxy_data_dir() == tmp_path / "cfg" / "catalpa" / "local-proxy"

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(local_proxy.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert (
        local_proxy.local_proxy_data_dir()
        == tmp_path / "home" / ".config" / "catalpa" / "local-proxy"
    )


def test_local_proxy_enabled_requires_local_docker() -> None:
    assert local_proxy_enabled(
        {
            "local_proxy": {"enabled": True, "upstream_port": 5555},
            "site_origin": "https://app-dev.localdev.temp.build",
        }
    )
    assert not local_proxy_enabled(
        {
            "docker_host": "ssh://host",
            "local_proxy": {"enabled": True, "upstream_port": 5555},
        }
    )
    assert not local_proxy_enabled({"local_proxy": {"enabled": False}})


def test_local_proxy_requires_upstream_port() -> None:
    with pytest.raises(LocalProxyConfigError, match="upstream_port"):
        local_proxy_upstream_dial(
            {
                "local_proxy": {"enabled": True},
                "site_origin": "https://app-dev.localdev.temp.build",
            }
        )


def test_local_proxy_hostname_from_site_origin() -> None:
    info = {
        "site_origin": "https://ambulancia-dev.localdev.temp.build",
        "local_proxy": {"enabled": True, "upstream_port": 5555},
    }
    assert local_proxy_hostname(info) == "ambulancia-dev.localdev.temp.build"
    assert local_proxy_upstream_dial(info) == "host.docker.internal:5555"


def test_local_proxy_custom_upstream_host() -> None:
    info = {
        "site_origin": "https://app-dev.localdev.temp.build",
        "local_proxy": {
            "enabled": True,
            "upstream_port": 8080,
            "upstream_host": "127.0.0.1",
        },
    }
    assert local_proxy_upstream_dial(info) == "127.0.0.1:8080"


def test_route_id_namespaced(minimal_config: ProjectConfig) -> None:
    assert route_id(minimal_config, "dev") == "local-proxy-minimal-dev"


def test_build_route_config() -> None:
    cfg = build_route_config(
        "local-proxy-ambulancia-dev",
        "ambulancia-dev.localdev.temp.build",
        "host.docker.internal:5555",
    )
    assert cfg["@id"] == "local-proxy-ambulancia-dev"
    assert cfg["match"] == [{"host": ["ambulancia-dev.localdev.temp.build"]}]
    assert cfg["handle"][0]["handler"] == "reverse_proxy"
    assert cfg["handle"][0]["upstreams"] == [{"dial": "host.docker.internal:5555"}]
    assert cfg["terminal"] is True


def test_upsert_route_dedupes_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    # Server already has a stale route for the same @id plus an unrelated route.
    server = {
        "listen": [":443"],
        "routes": [
            {"@id": "local-proxy-ambulancia-dev", "match": [{"host": ["old"]}]},
            {"@id": "local-proxy-other-dev", "match": [{"host": ["keep"]}]},
        ],
    }
    calls: list[tuple[str, str, dict | None]] = []

    def fake_admin_request(method, path, *, body=None, timeout=10.0):
        calls.append((method, path, body))
        if method == "GET" and path == "/config/apps/http/servers":
            return 200, json.dumps({"srv0": {"listen": [":443"]}})
        if method == "GET" and path == "/config/apps/http/servers/srv0":
            return 200, json.dumps(server)
        if method == "PATCH":
            return 200, ""
        raise AssertionError(f"unexpected admin call {method} {path}")

    monkeypatch.setattr(local_proxy, "_admin_request", fake_admin_request)

    rc = upsert_route(
        "local-proxy-ambulancia-dev",
        "ambulancia-dev.localdev.temp.build",
        "host.docker.internal:5555",
    )
    assert rc == 0

    patch_call = next(c for c in calls if c[0] == "PATCH")
    routes = patch_call[2]["routes"]
    ids = [r["@id"] for r in routes]
    # stale duplicate replaced (single entry), unrelated route preserved.
    assert ids.count("local-proxy-ambulancia-dev") == 1
    assert "local-proxy-other-dev" in ids
    new_route = next(r for r in routes if r["@id"] == "local-proxy-ambulancia-dev")
    assert new_route["match"] == [{"host": ["ambulancia-dev.localdev.temp.build"]}]
