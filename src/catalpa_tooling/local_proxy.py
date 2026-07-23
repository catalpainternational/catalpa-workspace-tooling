"""Machine-wide local dev HTTPS reverse proxy (Caddy + admin API)."""

from __future__ import annotations

import http.client
import json
import os
import platform
import re
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TextIO

from catalpa_tooling.local_proxy_assets import local_proxy_caddyfile_path
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.site_origin import (
    hostnames_from_origins,
    parse_site_origins_from_info,
    primary_site_origin_for_env,
    resolve_site_origins_for_env,
)

if TYPE_CHECKING:
    from catalpa_tooling.config import ProjectConfig

from catalpa_tooling.dev_lan_access import (
    detect_dev_lan_ipv4,
    ip_to_dns_label,
    lan_access_enabled,
    lan_dns_suffix_from_info,
    lan_hostname_for,
)

LOCAL_PROXY_CONTAINER = "catalpa-local-proxy"
LOCAL_PROXY_NETWORK = "catalpa-local-proxy-net"
# Legacy Docker named volume (pre host-persistence). Kept for migration/cleanup.
LOCAL_PROXY_VOLUME = "catalpa_local_proxy_data"
LOCAL_PROXY_ADMIN_URL = "http://127.0.0.1:2019"
CADDY_IMAGE = "caddy:2-alpine"
# Minimum Compose version for ``ports: !reset []`` in generated overrides.
LOCAL_PROXY_MIN_COMPOSE_VERSION = "2.24.0"
# Stable common-name prefix of the persisted local dev CA root, as it appears in
# the OS trust store (see the shipped local_proxy/Caddyfile pki block). The full
# CN is suffixed with a per-machine label, e.g. "Catalpa Local Dev Root (mymac)",
# so distinct machines' roots are distinguishable. Trust-store lookups match on
# this prefix (substring match), which still finds the machine-suffixed CN.
LOCAL_PROXY_CA_COMMON_NAME = "Catalpa Local Dev Root"
# Env var read by the bundled Caddyfile ({$CATALPA_LOCAL_DEV_MACHINE:local}) to
# label the minted CA with the machine that created it.
LOCAL_PROXY_CA_MACHINE_ENV = "CATALPA_LOCAL_DEV_MACHINE"
_ROUTE_ID_PREFIX = "local-proxy"
_ROUTE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_LAN_ROUTE_SUFFIX_RE = re.compile(r"-lan-(?P<ip>\d+(?:-\d+)*)$")
LOCAL_PROXY_UPSTREAM_PORT = 80


def local_proxy_data_dir() -> Path:
    """Host directory bind-mounted at ``/data`` so the CA persists across wipes.

    Uses ``$XDG_CONFIG_HOME/catalpa/local-proxy`` (default ``~/.config/...``),
    matching the tooling's shell-setup convention. Because this lives on the
    host rather than in a Docker volume, the local dev CA is minted once per
    machine and survives ``docker volume prune`` / proxy re-creation.
    """
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "catalpa" / "local-proxy"


def local_dev_ca_machine_label() -> str:
    """Short, sanitized label identifying the machine that mints the local dev CA.

    Honors an explicit ``CATALPA_LOCAL_DEV_MACHINE`` override; otherwise derives
    from the host's short hostname. Sanitized to ``[a-z0-9-]`` so it is safe to
    embed in the CA common name and pass as an env var. Falls back to ``local``.
    """
    override = os.environ.get(LOCAL_PROXY_CA_MACHINE_ENV, "").strip()
    raw = override or platform.node() or socket.gethostname() or ""
    short = raw.split(".")[0]
    label = re.sub(r"[^a-z0-9-]+", "-", short.strip().lower()).strip("-")
    return label or "local"


class LocalProxyRoute(NamedTuple):
    """One registered dev-proxy route (Caddy admin ``@id``, host, upstream dial)."""

    route_id: str
    host: str
    upstream_dial: str
    # Host header to send upstream. ``None`` preserves the incoming Host. LAN
    # routes set this to the canonical host so stack Caddy site blocks (and
    # Vite ``allowedHosts``) match without knowing the dynamic sslip hostname.
    upstream_host_header: str | None = None


class LocalProxyConfigError(ValueError):
    """Invalid ``local_proxy`` / ``site_origin`` configuration."""


def _local_proxy_block(info: dict[str, Any]) -> dict[str, Any]:
    raw = info.get("local_proxy")
    if raw is None or raw is False:
        return {}
    if not isinstance(raw, dict):
        raise LocalProxyConfigError("local_proxy must be a mapping")
    return raw


def _is_remote_docker_host(docker_host: object) -> bool:
    """True for SSH remote daemons; local socket / unset counts as local."""
    dh = str(docker_host or "").strip()
    return dh.startswith("ssh://")


def local_proxy_enabled(info: dict[str, Any]) -> bool:
    """True when this env uses the shared machine-wide HTTPS proxy (local Docker only)."""
    if _is_remote_docker_host(info.get("docker_host")):
        return False
    raw = info.get("local_proxy")
    if raw is False:
        return False
    if isinstance(raw, dict):
        return bool(raw.get("enabled", True))
    return True


def default_compose_project_name(config: ProjectConfig, env_name: str) -> str:
    """Default ``{compose_project_default}_{env}`` Compose project name."""
    from catalpa_tooling.restic_files import (
        _default_compose_project,
        _sanitize_dk_env_for_compose_project_suffix,
    )

    suffix = _sanitize_dk_env_for_compose_project_suffix(env_name)
    return f"{_default_compose_project(config)}_{suffix}"


def compose_project_name_from_info(
    info: dict[str, Any],
    config: ProjectConfig | None = None,
    env_name: str = "",
) -> str:
    """Return explicit or default Compose project name for local proxy routing."""
    env_block = info.get("env")
    if isinstance(env_block, dict):
        raw = env_block.get("compose_project_name")
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    if config is not None and env_name:
        return default_compose_project_name(config, env_name)
    raise LocalProxyConfigError(
        "info.env.compose_project_name is required when local_proxy is enabled "
        "and compose project name cannot be derived"
    )


def compose_project_name_from_env_add(
    env_add: dict[str, str],
    info: dict[str, Any] | None = None,
    *,
    config: ProjectConfig | None = None,
    env_name: str = "",
) -> str:
    """Resolve the Compose project name used for network aliases and overrides."""
    name = (env_add.get("COMPOSE_PROJECT_NAME") or "").strip()
    if name:
        return name
    if info is not None:
        return compose_project_name_from_info(info, config, env_name)
    raise LocalProxyConfigError("COMPOSE_PROJECT_NAME is required for local proxy routing")


def local_proxy_service(
    config: ProjectConfig,
    info: dict[str, Any] | None = None,
    *,
    route_index: int | None = None,
) -> str:
    """Compose front service that receives traffic from the machine-wide dev proxy."""
    _ = info, route_index
    return config.stack_service("proxy")


def local_proxy_upstream_alias(compose_project_name: str, service: str) -> str:
    """Stable DNS alias on ``LOCAL_PROXY_NETWORK`` (must be unique per machine)."""
    project = compose_project_name.strip()
    svc = service.strip()
    if not project or not svc:
        raise LocalProxyConfigError(
            "compose project name and local_proxy.service are required for upstream alias"
        )
    return f"{project}-{svc}"


def local_proxy_upstream_host(
    config: ProjectConfig,
    compose_project_name: str,
    info: dict[str, Any] | None = None,
    *,
    route_index: int | None = None,
    route: dict[str, Any] | None = None,
) -> str:
    """Dial target hostname: explicit ``upstream_host`` or ``{project}-{service}`` alias."""
    if route is not None:
        raw = route.get("upstream_host") or route.get("upstreamHost")
        if raw is not None:
            host = str(raw).strip()
            if host:
                return host
    if info is not None:
        block = _local_proxy_block(info)
        raw = block.get("upstream_host") or block.get("upstreamHost")
        if raw is not None:
            host = str(raw).strip()
            if host:
                return host
    service = local_proxy_service(config, info, route_index=route_index)
    return local_proxy_upstream_alias(compose_project_name, service)


def local_proxy_upstream_port(info: dict[str, Any] | None = None) -> int:
    _ = info
    return LOCAL_PROXY_UPSTREAM_PORT


def _parse_upstream_port(raw: object, *, field: str) -> int:
    try:
        port = int(str(raw).strip())
    except ValueError as e:
        raise LocalProxyConfigError(f"{field} must be an integer (got {raw!r})") from e
    if not 1 <= port <= 65535:
        raise LocalProxyConfigError(f"{field} out of range: {port}")
    return port


def local_proxy_upstream_dial(
    config: ProjectConfig,
    compose_project_name: str,
    info: dict[str, Any] | None = None,
) -> str:
    return (
        f"{local_proxy_upstream_host(config, compose_project_name, info)}:"
        f"{local_proxy_upstream_port(info)}"
    )


def local_proxy_hostname(
    info: dict[str, Any],
    config: ProjectConfig,
    env_name: str,
) -> str:
    origin = primary_site_origin_for_env(info, config, env_name)
    if not origin:
        raise LocalProxyConfigError(
            "Could not derive site_origin for local proxy "
            f"(set site_origin or configure project.name + env {env_name!r})"
        )
    hosts = hostnames_from_origins([origin])
    if not hosts:
        raise LocalProxyConfigError(f"Could not derive hostname from site_origin: {origin!r}")
    return hosts[0]


def _hostname_from_route_host(raw: object, *, field: str) -> str:
    host = str(raw).strip()
    if not host:
        raise LocalProxyConfigError(f"{field} must not be empty")
    if "://" in host:
        parsed_hosts = hostnames_from_origins([host if host.startswith("http") else f"https://{host}"])
        if not parsed_hosts:
            raise LocalProxyConfigError(f"Could not derive hostname from {field}: {raw!r}")
        return parsed_hosts[0]
    return host.split("/")[0]


def route_id_for_host(config: ProjectConfig, env_name: str, host: str) -> str:
    """Stable Caddy admin ``@id`` for one host within a multi-route environment."""
    project = _sanitize_route_label(config.meta.name, field="project name")
    env = _sanitize_route_label(env_name, field="env name")
    host_label = _sanitize_route_label(host, field="host")
    rid = f"{_ROUTE_ID_PREFIX}-{project}-{env}-{host_label}"
    if not _ROUTE_ID_RE.fullmatch(rid):
        raise LocalProxyConfigError(
            f"Could not build local proxy route id from {project!r}/{env_name!r}/{host!r}"
        )
    return rid


def _expand_lan_proxy_routes(
    entries: list[LocalProxyRoute],
    info: dict[str, Any],
    *,
    config: ProjectConfig | None = None,
) -> list[LocalProxyRoute]:
    """Add magic-DNS LAN host routes (same upstream) for each base route and LAN IPv4."""
    if not lan_access_enabled(info):
        return entries
    ips = detect_dev_lan_ipv4()
    if not ips:
        return entries
    suffix = lan_dns_suffix_from_info(info, config=config)
    out = list(entries)
    for entry in entries:
        for ip in ips:
            lan_host = lan_hostname_for(
                entry.host, ip, lan_dns_suffix=suffix, config=config
            )
            ip_slug = _sanitize_route_label(ip_to_dns_label(ip), field="ip")
            lan_rid = f"{entry.route_id}-lan-{ip_slug}"
            if not _ROUTE_ID_RE.fullmatch(lan_rid):
                continue
            out.append(
                LocalProxyRoute(
                    route_id=lan_rid,
                    host=lan_host,
                    upstream_dial=entry.upstream_dial,
                    # Send the canonical Host upstream so stack Caddy / Vite match
                    # their existing host config (matches how Vite already rewrites
                    # Host to SITE_ORIGIN when proxying to Django).
                    upstream_host_header=entry.host,
                )
            )
    return out


def local_proxy_routes(
    info: dict[str, Any],
    config: ProjectConfig,
    env_name: str,
    compose_project_name: str,
) -> list[LocalProxyRoute]:
    """Return all proxy routes for this environment (derived hostnames → stack caddy:80)."""
    if not local_proxy_enabled(info):
        return []

    upstream_dial = local_proxy_upstream_dial(config, compose_project_name, info)
    entries: list[LocalProxyRoute] = []
    for origin in resolve_site_origins_for_env(info, config, env_name):
        host = hostnames_from_origins([origin])[0]
        entries.append(
            LocalProxyRoute(
                route_id=route_id_for_host(config, env_name, host),
                host=host,
                upstream_dial=upstream_dial,
            )
        )
    return _expand_lan_proxy_routes(entries, info, config=config)


def _sanitize_route_label(label: str, *, field: str) -> str:
    s = label.strip().lower().replace("_", "-")
    s = re.sub(r"[^a-z0-9-]+", "-", s).strip("-")
    if not s:
        raise LocalProxyConfigError(f"Invalid {field} for local proxy route id: {label!r}")
    return s


def route_id(config: ProjectConfig, env_name: str) -> str:
    """Stable Caddy admin ``@id`` for one project environment."""
    project = _sanitize_route_label(config.meta.name, field="project name")
    env = _sanitize_route_label(env_name, field="env name")
    rid = f"{_ROUTE_ID_PREFIX}-{project}-{env}"
    if not _ROUTE_ID_RE.fullmatch(rid):
        raise LocalProxyConfigError(f"Could not build local proxy route id from {project!r}/{env!r}")
    return rid


def build_route_config(
    route_id_value: str,
    host: str,
    upstream_dial: str,
    *,
    upstream_host_header: str | None = None,
) -> dict[str, Any]:
    """JSON body for ``PUT /id/<route_id>`` on the Caddy admin API."""
    reverse_proxy: dict[str, Any] = {
        "handler": "reverse_proxy",
        "upstreams": [{"dial": upstream_dial}],
    }
    if upstream_host_header:
        reverse_proxy["headers"] = {
            "request": {"set": {"Host": [upstream_host_header]}}
        }
    return {
        "@id": route_id_value,
        "match": [{"host": [host]}],
        "handle": [reverse_proxy],
        "terminal": True,
    }


def _admin_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> tuple[int, str]:
    url = f"{LOCAL_PROXY_ADMIN_URL.rstrip('/')}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
            return resp.status, payload
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        return e.code, detail
    except (urllib.error.URLError, http.client.HTTPException, ConnectionError, OSError) as e:
        raise LocalProxyConfigError(
            f"Caddy admin API unreachable at {LOCAL_PROXY_ADMIN_URL} ({e}). "
            f"Start the proxy with `dk proxy up`."
        ) from e


def proxy_container_id() -> str:
    result = run_cmd(
        ["docker", "ps", "-q", "-f", f"name=^{LOCAL_PROXY_CONTAINER}$"],
        check=False,
        print_cmd=False,
        capture_output=True,
        text=True,
    )
    lines = (result.stdout or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def proxy_container_exists() -> bool:
    result = run_cmd(
        ["docker", "ps", "-aq", "-f", f"name=^{LOCAL_PROXY_CONTAINER}$"],
        check=False,
        print_cmd=False,
        capture_output=True,
        text=True,
    )
    return bool((result.stdout or "").strip())


def ensure_proxy_network(*, dry_run: bool = False) -> int:
    """Create the shared external network for project stacks and the dev proxy."""
    inspect = run_cmd(
        ["docker", "network", "inspect", LOCAL_PROXY_NETWORK],
        check=False,
        print_cmd=False,
        capture_output=True,
        text=True,
    )
    if inspect.returncode == 0:
        return 0
    if dry_run:
        print(
            f"dry-run: would create Docker network {LOCAL_PROXY_NETWORK!r}",
            file=sys.stderr,
        )
        return 0
    created = run_cmd(
        ["docker", "network", "create", LOCAL_PROXY_NETWORK],
        check=False,
        print_cmd=False,
    )
    return created.returncode


def _proxy_container_on_network() -> bool:
    cid = proxy_container_id()
    if not cid:
        return False
    result = run_cmd(
        ["docker", "inspect", cid, "--format", "{{json .NetworkSettings.Networks}}"],
        check=False,
        print_cmd=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    try:
        networks = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False
    return isinstance(networks, dict) and LOCAL_PROXY_NETWORK in networks


def ensure_proxy_on_network(*, dry_run: bool = False) -> int:
    """Attach a running ``catalpa-local-proxy`` to ``LOCAL_PROXY_NETWORK`` if needed."""
    if not proxy_container_id():
        return 0
    if _proxy_container_on_network():
        return 0
    if dry_run:
        print(
            f"dry-run: would connect {LOCAL_PROXY_CONTAINER!r} to {LOCAL_PROXY_NETWORK!r}",
            file=sys.stderr,
        )
        return 0
    connected = run_cmd(
        ["docker", "network", "connect", LOCAL_PROXY_NETWORK, LOCAL_PROXY_CONTAINER],
        check=False,
        print_cmd=False,
    )
    return connected.returncode


def ensure_proxy_running(*, dry_run: bool = False) -> int:
    """Start ``catalpa-local-proxy`` if it is not already running."""
    rc = ensure_proxy_network(dry_run=dry_run)
    if rc != 0:
        return rc

    if proxy_container_id():
        return ensure_proxy_on_network(dry_run=dry_run)

    caddyfile = local_proxy_caddyfile_path()
    if not caddyfile.is_file():
        print(f"Missing bundled Caddyfile: {caddyfile}", file=sys.stderr)
        return 1

    if dry_run:
        print(
            f"dry-run: would start {LOCAL_PROXY_CONTAINER!r} "
            f"(image {CADDY_IMAGE}, admin {LOCAL_PROXY_ADMIN_URL})",
            file=sys.stderr,
        )
        return 0

    if proxy_container_exists():
        print(f"Starting existing container {LOCAL_PROXY_CONTAINER!r}...", file=sys.stderr)
        start = run_cmd(["docker", "start", LOCAL_PROXY_CONTAINER], check=False)
        if start.returncode != 0:
            return start.returncode
        return ensure_proxy_on_network(dry_run=dry_run)

    data_dir = local_proxy_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        LOCAL_PROXY_CONTAINER,
        "--network",
        LOCAL_PROXY_NETWORK,
        "--add-host=host.docker.internal:host-gateway",
        "-e",
        f"{LOCAL_PROXY_CA_MACHINE_ENV}={local_dev_ca_machine_label()}",
        "-p",
        "80:80",
        "-p",
        "443:443",
        "-p",
        "127.0.0.1:2019:2019",
        "-v",
        f"{data_dir}:/data",
        "-v",
        f"{caddyfile}:/etc/caddy/Caddyfile:ro",
        CADDY_IMAGE,
        "caddy",
        "run",
        "--config",
        "/etc/caddy/Caddyfile",
    ]
    print(
        f"Starting local dev proxy {LOCAL_PROXY_CONTAINER!r} "
        f"(CA persisted at {data_dir})...",
        file=sys.stderr,
    )
    created = run_cmd(cmd, check=False)
    return created.returncode


def wait_for_proxy_admin(*, timeout: float = 15.0, interval: float = 0.3) -> bool:
    """Poll the Caddy admin API until it responds (freshly-started container race)."""
    import time

    deadline = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < deadline:
        try:
            status, _ = _admin_request("GET", "/config/", timeout=2.0)
            if status < 500:
                return True
        except LocalProxyConfigError as e:
            last_err = str(e)
        time.sleep(interval)
    if last_err:
        print(f"Local proxy admin API not ready after {timeout:.0f}s: {last_err}", file=sys.stderr)
    return False


def wait_for_ca_root(*, timeout: float = 10.0, interval: float = 0.5) -> bool:
    """Poll until Caddy's internal CA root exists in the running proxy container."""
    import time

    from catalpa_tooling.trust_caddy_cert import CADDY_LOCAL_CA_PATH

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cid = proxy_container_id()
        if cid:
            result = run_cmd(
                ["docker", "exec", cid, "test", "-f", CADDY_LOCAL_CA_PATH],
                check=False,
                print_cmd=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return True
        time.sleep(interval)
    return False


def stop_proxy(*, dry_run: bool = False) -> int:
    """Stop and remove ``catalpa-local-proxy`` (does not delete the data volume)."""
    if not proxy_container_exists():
        if dry_run:
            print(f"dry-run: {LOCAL_PROXY_CONTAINER!r} is not present.", file=sys.stderr)
        return 0
    if dry_run:
        print(f"dry-run: would stop and remove {LOCAL_PROXY_CONTAINER!r}", file=sys.stderr)
        return 0
    run_cmd(["docker", "rm", "-f", LOCAL_PROXY_CONTAINER], check=False)
    return 0


def _https_server_name() -> str:
    """Return the Caddy server (e.g. ``srv0``) that listens on :443."""
    status, payload = _admin_request("GET", "/config/apps/http/servers")
    if status >= 400:
        raise LocalProxyConfigError(
            f"Caddy admin API returned HTTP {status} reading servers: {payload}"
        )
    data = json.loads(payload) if payload.strip() else {}
    if not isinstance(data, dict):
        raise LocalProxyConfigError("Unexpected servers config from Caddy admin API")
    for name, server in data.items():
        if isinstance(server, dict):
            listen = server.get("listen") or []
            if any(str(entry).endswith(":443") for entry in listen):
                return name
    raise LocalProxyConfigError(
        "No Caddy server listening on :443 found; is the proxy bootstrap config loaded?"
    )


def upsert_route(
    route_id_value: str,
    host: str,
    upstream_dial: str,
    *,
    upstream_host_header: str | None = None,
    dry_run: bool = False,
) -> int:
    body = build_route_config(
        route_id_value,
        host,
        upstream_dial,
        upstream_host_header=upstream_host_header,
    )
    if dry_run:
        rewrite = f" host_header={upstream_host_header!r}" if upstream_host_header else ""
        print(
            f"dry-run: would upsert route /id/{route_id_value} "
            f"host={host!r} upstream={upstream_dial!r}{rewrite}",
            file=sys.stderr,
        )
        return 0

    server_name = _https_server_name()
    server_path = f"/config/apps/http/servers/{server_name}"
    status, payload = _admin_request("GET", server_path)
    if status >= 400:
        print(
            f"Failed to read local proxy server {server_name!r} (HTTP {status}): {payload}",
            file=sys.stderr,
        )
        return 1
    server = json.loads(payload) if payload.strip() else {}
    if not isinstance(server, dict):
        server = {}
    routes = server.get("routes")
    if not isinstance(routes, list):
        routes = []
    routes = [
        r for r in routes if not (isinstance(r, dict) and r.get("@id") == route_id_value)
    ]
    routes.append(body)
    server["routes"] = routes

    status, detail = _admin_request("PATCH", server_path, body=server)
    if status >= 400:
        print(
            f"Failed to register local proxy route {route_id_value!r} "
            f"(HTTP {status}): {detail}",
            file=sys.stderr,
        )
        return 1
    return 0


def remove_route(route_id_value: str, *, dry_run: bool = False) -> int:
    if dry_run:
        print(f"dry-run: would DELETE /id/{route_id_value}", file=sys.stderr)
        return 0
    # Best-effort teardown: if the shared proxy is not running there is nothing
    # to remove, and `down` should not fail because of it.
    if not proxy_container_id():
        return 0
    try:
        status, detail = _admin_request("DELETE", f"/id/{route_id_value}")
    except LocalProxyConfigError as e:
        print(f"Note: could not remove local proxy route {route_id_value!r}: {e}", file=sys.stderr)
        return 0
    if status in (200, 204, 404):
        return 0
    print(
        f"Failed to remove local proxy route {route_id_value!r} (HTTP {status}): {detail}",
        file=sys.stderr,
    )
    return 1


def _route_host_from_config(route: dict[str, Any]) -> str:
    match = route.get("match")
    if isinstance(match, list) and match:
        first = match[0]
        if isinstance(first, dict):
            hosts = first.get("host")
            if isinstance(hosts, list) and hosts:
                return str(hosts[0])
    return ""


def _route_upstream_from_config(route: dict[str, Any]) -> str:
    handle = route.get("handle")
    if isinstance(handle, list) and handle:
        first = handle[0]
        if isinstance(first, dict) and first.get("handler") == "reverse_proxy":
            upstreams = first.get("upstreams")
            if isinstance(upstreams, list) and upstreams:
                dial = upstreams[0].get("dial") if isinstance(upstreams[0], dict) else None
                if dial:
                    return str(dial)
    return ""


def _parse_route_id_metadata(route_id_value: str, host: str) -> tuple[str, str, bool]:
    """Best-effort ``(project, env, is_lan)`` from a route ``@id`` and matched host.

    LAN routes append ``-lan-<ip-slug>`` to the base id (see ``_expand_lan_proxy_routes``).
    """
    prefix = f"{_ROUTE_ID_PREFIX}-"
    if not route_id_value.startswith(prefix):
        return route_id_value, "", False
    base = route_id_value[len(prefix) :]
    is_lan = False
    lan_match = _LAN_ROUTE_SUFFIX_RE.search(base)
    if lan_match:
        is_lan = True
        base = base[: lan_match.start()]
    host_suffix = _sanitize_route_label(host, field="host") if host else ""
    if host_suffix and base.endswith(f"-{host_suffix}"):
        base = base[: -(len(host_suffix) + 1)]
    parts = base.rsplit("-", 1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1], is_lan
    return base or route_id_value, "", is_lan


def _project_env_from_id_and_host(route_id_value: str, host: str) -> tuple[str, str]:
    """Best-effort ``(project, env)`` from a route ``@id`` and matched host."""
    project, env, _ = _parse_route_id_metadata(route_id_value, host)
    return project, env


def _parent_route_id(route_id_value: str) -> str | None:
    """Canonical route ``@id`` for a LAN sibling (strip ``-lan-<ip-slug>`` suffix)."""
    lan_match = _LAN_ROUTE_SUFFIX_RE.search(route_id_value)
    if not lan_match:
        return None
    return route_id_value[: lan_match.start()]


def _https_server_routes() -> list[dict[str, Any]]:
    server_name = _https_server_name()
    status, payload = _admin_request("GET", f"/config/apps/http/servers/{server_name}/routes")
    if status >= 400:
        raise LocalProxyConfigError(
            f"Caddy admin API returned HTTP {status} reading routes: {payload}"
        )
    data = json.loads(payload) if payload.strip() else []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def proxy_status_lines() -> list[str]:
    lines: list[str] = []
    cid = proxy_container_id()
    if not cid:
        lines.append(f"{LOCAL_PROXY_CONTAINER}: not running")
        return lines
    lines.append(f"{LOCAL_PROXY_CONTAINER}: running ({cid[:12]})")
    lines.append(f"admin: {LOCAL_PROXY_ADMIN_URL}")
    try:
        routes = _https_server_routes()
        # Group by project, then env, preserving first-seen order at each level.
        grouped: dict[str, dict[str, list[tuple[bool, str, str]]]] = {}
        pending: list[tuple[str, str, str, str, str, bool]] = []
        canonical_project_env: dict[str, tuple[str, str]] = {}
        for route in routes:
            rid = route.get("@id")
            if not isinstance(rid, str) or not rid.startswith(f"{_ROUTE_ID_PREFIX}-"):
                continue
            host = _route_host_from_config(route)
            upstream = _route_upstream_from_config(route)
            project, env, is_lan = _parse_route_id_metadata(rid, host)
            pending.append((rid, host, upstream, project, env, is_lan))
            if not is_lan:
                canonical_project_env[rid] = (project, env)
        for rid, host, upstream, project, env, is_lan in pending:
            if is_lan:
                parent_id = _parent_route_id(rid)
                if parent_id and parent_id in canonical_project_env:
                    project, env = canonical_project_env[parent_id]
            grouped.setdefault(project, {}).setdefault(env, []).append(
                (is_lan, host, upstream)
            )
        if grouped:
            lines.append("live sites:")
            for project, envs in grouped.items():
                lines.append(f"  {project}:")
                for env, sites in envs.items():
                    lines.append(f"    {env}:" if env else "    (unknown):")
                    local_sites = sorted(
                        ((host, upstream) for is_lan, host, upstream in sites if not is_lan),
                        key=lambda row: row[0].lower(),
                    )
                    lan_sites = sorted(
                        ((host, upstream) for is_lan, host, upstream in sites if is_lan),
                        key=lambda row: row[0].lower(),
                    )
                    if local_sites:
                        lines.append("      local:")
                        for host, upstream in local_sites:
                            lines.append(f"        {host} -> {upstream}")
                    if lan_sites:
                        lines.append("      lan:")
                        for host, upstream in lan_sites:
                            lines.append(f"        {host} -> {upstream}")
        else:
            lines.append("live sites: (none)")
    except (LocalProxyConfigError, json.JSONDecodeError) as e:
        lines.append(f"live sites: {e}")
    return lines


def print_proxy_status(*, file: TextIO | None = None) -> None:
    out = sys.stderr if file is None else file
    for line in proxy_status_lines():
        print(line, file=out)


def local_proxy_public_url(info: dict[str, Any], config: ProjectConfig, env_name: str) -> str:
    origins = parse_site_origins_from_info(info)
    if origins:
        return origins[0]
    return primary_site_origin_for_env(info, config, env_name) or ""


def print_local_proxy_url(
    info: dict[str, Any],
    config: ProjectConfig,
    env_name: str,
    *,
    file: TextIO | None = None,
) -> None:
    out = sys.stderr if file is None else file
    origins = resolve_site_origins_for_env(info, config, env_name)
    for origin in origins:
        print(f"Local dev URL: {origin}", file=out)


def ca_is_trusted() -> bool:
    """Best-effort check whether Caddy's local CA appears trusted on this machine.

    Matches the trust store on ``LOCAL_PROXY_CA_COMMON_NAME`` (the stable prefix),
    which ``security find-certificate -c`` treats as a common-name substring, so
    it still finds the machine-suffixed CN ``Catalpa Local Dev Root (<machine>)``.
    """
    system = platform.system()
    if system == "Darwin":
        result = run_cmd(
            [
                "security",
                "find-certificate",
                "-c",
                LOCAL_PROXY_CA_COMMON_NAME,
                "/Library/Keychains/System.keychain",
            ],
            check=False,
            print_cmd=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    if system == "Linux":
        cert_dir = Path("/usr/local/share/ca-certificates")
        if not cert_dir.is_dir():
            return False
        return any(
            p.is_file() and "catalpa" in p.name.lower() and p.suffix == ".crt"
            for p in cert_dir.iterdir()
        )
    return False


def print_ca_trust_hint(env_name: str, *, file: TextIO | None = None) -> None:
    if ca_is_trusted():
        return
    out = sys.stderr if file is None else file
    print(
        f"Trust the local dev CA once: `dk {env_name} trust-caddy-cert` "
        f"(or `dk proxy trust`)",
        file=out,
    )


def local_proxy_override_path(
    config: ProjectConfig,
    env_name: str,
    compose_project_name: str | None = None,
) -> Path:
    project = _sanitize_route_label(config.meta.name, field="project name")
    env = _sanitize_route_label(env_name, field="env name")
    if compose_project_name:
        cpn = _sanitize_route_label(compose_project_name, field="compose project")
        return local_proxy_data_dir() / "overrides" / f"{project}-{env}-{cpn}.yaml"
    return local_proxy_data_dir() / "overrides" / f"{project}-{env}.yaml"


def local_proxy_front_services(
    config: ProjectConfig,
    compose_project_name: str,
    info: dict[str, Any] | None = None,
) -> list[tuple[str, str]]:
    """Return ``(compose_service, network_alias)`` for the stack front proxy service."""
    service = local_proxy_service(config, info)
    alias = local_proxy_upstream_host(config, compose_project_name, info)
    return [(service, alias)]


def write_local_proxy_override(
    config: ProjectConfig,
    env_name: str,
    info: dict[str, Any],
    compose_project_name: str,
) -> Path:
    """Write a compose override that joins front services to the shared dev proxy network.

    Uses ``ports: !reset []`` to drop host port publishing from the base compose file.
    Requires Docker Compose 2.24+.
    """
    path = local_proxy_override_path(config, env_name, compose_project_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    services = local_proxy_front_services(config, compose_project_name, info)

    lines = [
        f"# Generated by catalpa-workspace-tooling for dk {env_name} (local dev proxy).",
        f"# Requires Docker Compose {LOCAL_PROXY_MIN_COMPOSE_VERSION}+ for ports: !reset [].",
        "networks:",
        f"  {LOCAL_PROXY_NETWORK}:",
        "    external: true",
        f"    name: {LOCAL_PROXY_NETWORK}",
        "services:",
    ]
    for service_name, alias in services:
        lines.extend(
            [
                f"  {service_name}:",
                "    ports: !reset []",
                "    networks:",
                "      default: null",
                f"      {LOCAL_PROXY_NETWORK}:",
                "        aliases:",
                f"          - {alias}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path.resolve()


def local_proxy_extra_compose_files(
    info: dict[str, Any],
    config: ProjectConfig,
    env_name: str,
    env_add: dict[str, str],
    compose_args: list[str],
) -> list[str]:
    """Return generated override path(s) when compose should attach to the dev proxy network."""
    if not local_proxy_enabled(info) or not compose_args:
        return []
    verb = compose_args[0]
    if verb not in ("up", "down", "restart"):
        return []
    project_name = compose_project_name_from_env_add(
        env_add, info, config=config, env_name=env_name
    )
    override = write_local_proxy_override(config, env_name, info, project_name)
    return [str(override)]


def sync_local_proxy_for_compose_action(
    info: dict[str, Any],
    config: ProjectConfig,
    env_name: str,
    compose_args: list[str],
    env_add: dict[str, str],
    *,
    dry_run: bool = False,
) -> int:
    """Ensure proxy + route(s) on ``up``; remove route(s) on ``down``."""
    if not local_proxy_enabled(info):
        return 0

    compose_project_name = compose_project_name_from_env_add(
        env_add, info, config=config, env_name=env_name
    )
    routes = local_proxy_routes(info, config, env_name, compose_project_name)
    if not compose_args:
        return 0

    verb = compose_args[0]
    if verb == "up":
        rc = ensure_proxy_network(dry_run=dry_run)
        if rc != 0:
            return rc
        rc = ensure_proxy_running(dry_run=dry_run)
        if rc != 0:
            return rc
        if not dry_run and not wait_for_proxy_admin():
            print(
                "Local proxy admin API did not become ready; retry `dk proxy up` "
                "or re-run once the proxy is up.",
                file=sys.stderr,
            )
            return 1
        for entry in routes:
            rc = upsert_route(
                entry.route_id,
                entry.host,
                entry.upstream_dial,
                upstream_host_header=entry.upstream_host_header,
                dry_run=dry_run,
            )
            if rc != 0:
                return rc
        print_local_proxy_url(info, config, env_name)
        print_ca_trust_hint(env_name)
        return 0

    if verb == "down":
        rc = 0
        for entry in routes:
            route_rc = remove_route(entry.route_id, dry_run=dry_run)
            if route_rc != 0:
                rc = route_rc
        return rc

    return 0
