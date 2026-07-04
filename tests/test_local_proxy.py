"""Tests for catalpa_tooling.local_proxy."""

from __future__ import annotations

import pytest

from catalpa_tooling import local_proxy
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.local_proxy import (
    LOCAL_PROXY_CA_COMMON_NAME,
    LOCAL_PROXY_CA_MACHINE_ENV,
    LOCAL_PROXY_NETWORK,
    LocalProxyConfigError,
    LocalProxyRoute,
    build_route_config,
    compose_project_name_from_env_add,
    ensure_proxy_network,
    ensure_proxy_on_network,
    local_dev_ca_machine_label,
    local_proxy_enabled,
    local_proxy_front_services,
    local_proxy_hostname,
    local_proxy_routes,
    local_proxy_upstream_alias,
    local_proxy_upstream_dial,
    proxy_status_lines,
    route_id,
    route_id_for_host,
    upsert_route,
    write_local_proxy_override,
)
from catalpa_tooling.local_proxy_assets import local_proxy_caddyfile_path

_DEV_INFO = {
    "site_origin": "https://ambulancia-dev.localdev.temp.build",
    "local_proxy": {"enabled": True, "service": "node", "upstream_port": 5555},
    "env": {"compose_project_name": "ambulancia"},
}

_FULL_INFO = {
    "site_origin": "https://ambulancia-full.localdev.temp.build",
    "local_proxy": {
        "enabled": True,
        "service": "caddy",
        "routes": [
            {"upstream_port": 80},
            {
                "host": "metabase.ambulancia-full.localdev.temp.build",
                "upstream_port": 80,
            },
        ],
    },
    "env": {"compose_project_name": "ambulancia-full"},
}


def test_local_proxy_caddyfile_shipped() -> None:
    path = local_proxy_caddyfile_path()
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "local_certs" in text
    # Admin must bind to all interfaces inside the container so the loopback-only
    # published port (-p 127.0.0.1:2019:2019) can reach it.
    assert "admin 0.0.0.0:2019" in text
    # Named CA so the persisted root is recognizable in the OS trust store, with
    # a per-machine suffix substituted at Caddyfile-adaptation time.
    assert "pki" in text
    assert f'root_cn "{LOCAL_PROXY_CA_COMMON_NAME} ({{$CATALPA_LOCAL_DEV_MACHINE:local}})"' in text
    assert "/catalpa-local-ca.crt" in text


def test_local_proxy_data_dir_respects_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert local_proxy.local_proxy_data_dir() == tmp_path / "cfg" / "catalpa" / "local-proxy"

    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(local_proxy.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert (
        local_proxy.local_proxy_data_dir()
        == tmp_path / "home" / ".config" / "catalpa" / "local-proxy"
    )


def test_local_dev_ca_machine_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOCAL_PROXY_CA_MACHINE_ENV, "My Mac.local")
    assert local_dev_ca_machine_label() == "my-mac"

    monkeypatch.delenv(LOCAL_PROXY_CA_MACHINE_ENV, raising=False)
    monkeypatch.setattr(local_proxy.platform, "node", lambda: "Peters-MBP.lan")
    assert local_dev_ca_machine_label() == "peters-mbp"

    monkeypatch.setattr(local_proxy.platform, "node", lambda: "")
    monkeypatch.setattr(local_proxy.socket, "gethostname", lambda: "")
    assert local_dev_ca_machine_label() == "local"


def test_ensure_proxy_running_passes_machine_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOCAL_PROXY_CA_MACHINE_ENV, "testbox")
    monkeypatch.setattr(local_proxy, "ensure_proxy_network", lambda **_: 0)
    monkeypatch.setattr(local_proxy, "proxy_container_id", lambda: "")
    monkeypatch.setattr(local_proxy, "proxy_container_exists", lambda: False)
    monkeypatch.setattr(
        local_proxy, "local_proxy_caddyfile_path", lambda: local_proxy_caddyfile_path()
    )

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        import subprocess

        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(local_proxy, "run_cmd", fake_run)

    assert local_proxy.ensure_proxy_running() == 0
    cmd = captured["cmd"]
    assert "-e" in cmd
    assert f"{LOCAL_PROXY_CA_MACHINE_ENV}=testbox" in cmd


def test_local_proxy_enabled_requires_local_docker() -> None:
    assert local_proxy_enabled(
        {
            "local_proxy": {"enabled": True, "service": "node", "upstream_port": 5555},
            "site_origin": "https://app-dev.localdev.temp.build",
            "env": {"compose_project_name": "myapp"},
        }
    )
    assert not local_proxy_enabled(
        {
            "docker_host": "ssh://host",
            "local_proxy": {"enabled": True, "service": "node", "upstream_port": 5555},
        }
    )
    assert local_proxy_enabled(
        {
            "docker_host": "unix:///var/run/docker.sock",
            "local_proxy": {"enabled": True, "service": "caddy", "upstream_port": 80},
            "site_origin": "https://app-full.localdev.temp.build",
            "env": {"compose_project_name": "myapp-full"},
        }
    )
    assert not local_proxy_enabled({"local_proxy": {"enabled": False}})


def test_local_proxy_requires_upstream_port() -> None:
    with pytest.raises(LocalProxyConfigError, match="upstream_port"):
        local_proxy_upstream_dial(
            {
                "local_proxy": {"enabled": True, "service": "node"},
                "site_origin": "https://app-dev.localdev.temp.build",
                "env": {"compose_project_name": "myapp"},
            },
            "myapp",
        )


def test_local_proxy_requires_service() -> None:
    with pytest.raises(LocalProxyConfigError, match="local_proxy.service"):
        local_proxy_upstream_dial(
            {
                "local_proxy": {"enabled": True, "upstream_port": 5555},
                "site_origin": "https://app-dev.localdev.temp.build",
                "env": {"compose_project_name": "myapp"},
            },
            "myapp",
        )


def test_local_proxy_hostname_from_site_origin() -> None:
    assert local_proxy_hostname(_DEV_INFO) == "ambulancia-dev.localdev.temp.build"
    assert local_proxy_upstream_dial(_DEV_INFO, "ambulancia") == "ambulancia-node:5555"


def test_local_proxy_upstream_alias() -> None:
    assert local_proxy_upstream_alias("ambulancia-full", "caddy") == "ambulancia-full-caddy"


def test_local_proxy_custom_upstream_host() -> None:
    info = {
        "site_origin": "https://app-dev.localdev.temp.build",
        "local_proxy": {
            "enabled": True,
            "service": "node",
            "upstream_port": 8080,
            "upstream_host": "custom-host.example",
        },
        "env": {"compose_project_name": "myapp"},
    }
    assert local_proxy_upstream_dial(info, "myapp") == "custom-host.example:8080"


def test_compose_project_name_from_env_add() -> None:
    assert compose_project_name_from_env_add({"COMPOSE_PROJECT_NAME": "from-env"}) == "from-env"
    assert compose_project_name_from_env_add({}, _DEV_INFO) == "ambulancia"


def test_route_id_namespaced(minimal_config: ProjectConfig) -> None:
    assert route_id(minimal_config, "dev") == "local-proxy-minimal-dev"


def test_build_route_config() -> None:
    cfg = build_route_config(
        "local-proxy-ambulancia-dev",
        "ambulancia-dev.localdev.temp.build",
        "ambulancia-node:5555",
    )
    assert cfg["@id"] == "local-proxy-ambulancia-dev"
    assert cfg["match"] == [{"host": ["ambulancia-dev.localdev.temp.build"]}]
    assert cfg["handle"][0]["handler"] == "reverse_proxy"
    assert cfg["handle"][0]["upstreams"] == [{"dial": "ambulancia-node:5555"}]
    assert "headers" not in cfg["handle"][0]
    assert cfg["terminal"] is True


def test_build_route_config_host_rewrite() -> None:
    cfg = build_route_config(
        "local-proxy-ambulancia-dev-lan-192-168-1-42",
        "ambulancia-dev.192-168-1-42.sslip.io",
        "ambulancia-node:5555",
        upstream_host_header="ambulancia-dev.localdev.temp.build",
    )
    assert cfg["match"] == [{"host": ["ambulancia-dev.192-168-1-42.sslip.io"]}]
    assert cfg["handle"][0]["headers"]["request"]["set"]["Host"] == [
        "ambulancia-dev.localdev.temp.build"
    ]


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
        "ambulancia-node:5555",
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


def test_local_proxy_routes_legacy_single(minimal_config: ProjectConfig) -> None:
    routes = local_proxy_routes(_DEV_INFO, minimal_config, "dev", "ambulancia")
    assert routes == [
        LocalProxyRoute(
            route_id="local-proxy-minimal-dev",
            host="ambulancia-dev.localdev.temp.build",
            upstream_dial="ambulancia-node:5555",
        )
    ]


def test_local_proxy_routes_multi(minimal_config: ProjectConfig) -> None:
    routes = local_proxy_routes(_FULL_INFO, minimal_config, "full", "ambulancia-full")
    assert len(routes) == 2
    assert routes[0].host == "ambulancia-full.localdev.temp.build"
    assert routes[0].upstream_dial == "ambulancia-full-caddy:80"
    assert routes[0].route_id == route_id_for_host(
        minimal_config, "full", "ambulancia-full.localdev.temp.build"
    )
    assert routes[1].host == "metabase.ambulancia-full.localdev.temp.build"
    assert routes[1].upstream_dial == "ambulancia-full-caddy:80"


def test_local_proxy_routes_lan_expansion(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = {
        **_DEV_INFO,
        "local_proxy": {**_DEV_INFO["local_proxy"], "lan_access": True},
    }
    monkeypatch.setattr(local_proxy, "detect_dev_lan_ipv4", lambda: ["192.168.1.42"])
    routes = local_proxy_routes(info, minimal_config, "dev", "ambulancia")
    assert len(routes) == 2
    assert routes[0].host == "ambulancia-dev.localdev.temp.build"
    assert routes[0].upstream_host_header is None
    assert routes[1].host == "ambulancia-dev.192-168-1-42.sslip.io"
    assert routes[1].upstream_dial == routes[0].upstream_dial
    assert routes[1].upstream_host_header == "ambulancia-dev.localdev.temp.build"
    assert routes[1].route_id.endswith("-lan-192-168-1-42")


def test_route_id_for_host(minimal_config: ProjectConfig) -> None:
    rid = route_id_for_host(minimal_config, "full", "metabase.ambulancia-full.localdev.temp.build")
    assert rid.startswith("local-proxy-minimal-full-")


def test_local_proxy_front_services_dedupes_same_service() -> None:
    services = local_proxy_front_services(_FULL_INFO, "ambulancia-full")
    assert services == [("caddy", "ambulancia-full-caddy")]


def test_write_local_proxy_override(
    minimal_config: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = write_local_proxy_override(minimal_config, "dev", _DEV_INFO, "ambulancia")
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert f"name: {LOCAL_PROXY_NETWORK}" in text
    assert "node:" in text
    assert "ports: !reset []" in text
    assert "ambulancia-node" in text
    assert "default: null" in text


def test_ensure_proxy_network_creates_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        class Result:
            returncode = 1 if cmd[:3] == ["docker", "network", "inspect"] else 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(local_proxy, "run_cmd", fake_run)
    assert ensure_proxy_network() == 0
    assert ["docker", "network", "create", LOCAL_PROXY_NETWORK] in calls


def test_ensure_proxy_on_network_connects_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_proxy, "proxy_container_id", lambda: "abc123")
    monkeypatch.setattr(local_proxy, "_proxy_container_on_network", lambda: False)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(local_proxy, "run_cmd", fake_run)
    assert ensure_proxy_on_network() == 0
    assert [
        "docker",
        "network",
        "connect",
        LOCAL_PROXY_NETWORK,
        local_proxy.LOCAL_PROXY_CONTAINER,
    ] in calls


def test_parse_route_id_metadata_lan_suffix() -> None:
    project, env, is_lan = local_proxy._parse_route_id_metadata(
        "local-proxy-ambulancia-dev-lan-192-168-1-185",
        "ambulancia-dev.192-168-1-185.lan.localdev.temp.build",
    )
    assert project == "ambulancia"
    assert env == "dev"
    assert is_lan is True


def test_proxy_status_lines_groups_lan_under_same_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setattr(local_proxy, "proxy_container_id", lambda: "abc123def456")
    monkeypatch.setattr(local_proxy, "_https_server_name", lambda: "srv0")

    def fake_admin_request(method, path, *, body=None, timeout=10.0):
        if method == "GET" and path == "/config/apps/http/servers/srv0/routes":
            return 200, json.dumps(
                [
                    {
                        "@id": "local-proxy-ambulancia-dev",
                        "match": [{"host": ["ambulancia-dev.localdev.temp.build"]}],
                        "handle": [
                            {
                                "handler": "reverse_proxy",
                                "upstreams": [{"dial": "ambulancia-node:5555"}],
                            }
                        ],
                    },
                    {
                        "@id": "local-proxy-ambulancia-dev-lan-192-168-1-185",
                        "match": [
                            {
                                "host": [
                                    "ambulancia-dev.192-168-1-185.lan.localdev.temp.build"
                                ]
                            }
                        ],
                        "handle": [
                            {
                                "handler": "reverse_proxy",
                                "upstreams": [{"dial": "ambulancia-node:5555"}],
                            }
                        ],
                    },
                ]
            )
        raise AssertionError(f"unexpected admin call {method} {path}")

    monkeypatch.setattr(local_proxy, "_admin_request", fake_admin_request)

    lines = local_proxy.proxy_status_lines()
    assert "  ambulancia:" in lines
    assert "    dev:" in lines
    assert lines.count("    dev:") == 1
    assert "      ambulancia-dev.localdev.temp.build -> ambulancia-node:5555" in lines
    assert (
        "      ambulancia-dev.192-168-1-185.lan.localdev.temp.build (LAN) -> ambulancia-node:5555"
        in lines
    )
    assert not any("ambulancia-dev-lan" in line for line in lines)


def test_proxy_status_lines_lists_live_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setattr(local_proxy, "proxy_container_id", lambda: "abc123def456")
    monkeypatch.setattr(local_proxy, "_https_server_name", lambda: "srv0")

    def fake_admin_request(method, path, *, body=None, timeout=10.0):
        if method == "GET" and path == "/config/apps/http/servers/srv0/routes":
            return 200, json.dumps(
                [
                    {
                        "@id": "local-proxy-ambulancia-dev",
                        "match": [{"host": ["ambulancia-dev.localdev.temp.build"]}],
                        "handle": [
                            {
                                "handler": "reverse_proxy",
                                "upstreams": [{"dial": "ambulancia-node:5555"}],
                            }
                        ],
                    },
                    {
                        "@id": "local-proxy-ambulancia-full-metabase-ambulancia-full-localdev-temp-build",
                        "match": [{"host": ["metabase.ambulancia-full.localdev.temp.build"]}],
                        "handle": [
                            {
                                "handler": "reverse_proxy",
                                "upstreams": [{"dial": "ambulancia-full-caddy:80"}],
                            }
                        ],
                    },
                    {
                        "match": [{"host": ["redirect"]}],
                        "handle": [{"handler": "static_response"}],
                    },
                ]
            )
        raise AssertionError(f"unexpected admin call {method} {path}")

    monkeypatch.setattr(local_proxy, "_admin_request", fake_admin_request)

    lines = proxy_status_lines()
    # Grouped hierarchically: project -> env -> host -> upstream (no inline context).
    assert "live sites:" in lines
    assert "  ambulancia:" in lines
    assert "    dev:" in lines
    assert "    full:" in lines
    assert "      ambulancia-dev.localdev.temp.build -> ambulancia-node:5555" in lines
    assert (
        "      metabase.ambulancia-full.localdev.temp.build -> ambulancia-full-caddy:80"
        in lines
    )
    # Project/env context is now conveyed by indentation, not an inline suffix.
    assert not any("(ambulancia/dev)" in line for line in lines)
    assert not any("redirect" in line for line in lines)
