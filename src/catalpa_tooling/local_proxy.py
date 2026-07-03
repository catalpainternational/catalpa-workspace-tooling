"""Machine-wide local dev HTTPS reverse proxy (Caddy + admin API)."""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TextIO

from catalpa_tooling.local_proxy_assets import local_proxy_caddyfile_path
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.site_origin import hostnames_from_origins, parse_site_origins_from_info

if TYPE_CHECKING:
    from catalpa_tooling.config import ProjectConfig

LOCAL_PROXY_CONTAINER = "catalpa-local-proxy"
# Legacy Docker named volume (pre host-persistence). Kept for migration/cleanup.
LOCAL_PROXY_VOLUME = "catalpa_local_proxy_data"
LOCAL_PROXY_ADMIN_URL = "http://127.0.0.1:2019"
CADDY_IMAGE = "caddy:2-alpine"
DEFAULT_UPSTREAM_HOST = "host.docker.internal"
# Human-facing common name of the persisted local dev CA root, as it appears in
# the OS trust store (see the shipped local_proxy/Caddyfile pki block).
LOCAL_PROXY_CA_COMMON_NAME = "Catalpa Local Dev Root"
_ROUTE_ID_PREFIX = "local-proxy"
_ROUTE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


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


class LocalProxyRoute(NamedTuple):
    """One registered dev-proxy route (Caddy admin ``@id``, host, upstream dial)."""

    route_id: str
    host: str
    upstream_dial: str


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
    block = _local_proxy_block(info)
    return bool(block.get("enabled", False))


def local_proxy_upstream_host(info: dict[str, Any]) -> str:
    block = _local_proxy_block(info)
    raw = block.get("upstream_host") or block.get("upstreamHost") or DEFAULT_UPSTREAM_HOST
    host = str(raw).strip()
    if not host:
        raise LocalProxyConfigError("local_proxy.upstream_host must not be empty")
    return host


def local_proxy_upstream_port(info: dict[str, Any]) -> int:
    block = _local_proxy_block(info)
    raw = block.get("upstream_port") or block.get("upstreamPort")
    if raw is None:
        raise LocalProxyConfigError(
            "local_proxy.upstream_port is required when local_proxy.enabled is true "
            "and local_proxy.routes is not set"
        )
    return _parse_upstream_port(raw, field="local_proxy.upstream_port")


def _parse_upstream_port(raw: object, *, field: str) -> int:
    try:
        port = int(str(raw).strip())
    except ValueError as e:
        raise LocalProxyConfigError(f"{field} must be an integer (got {raw!r})") from e
    if not 1 <= port <= 65535:
        raise LocalProxyConfigError(f"{field} out of range: {port}")
    return port


def local_proxy_upstream_dial(info: dict[str, Any]) -> str:
    return f"{local_proxy_upstream_host(info)}:{local_proxy_upstream_port(info)}"


def local_proxy_hostname(info: dict[str, Any]) -> str:
    origins = parse_site_origins_from_info(info)
    if not origins:
        raise LocalProxyConfigError(
            "site_origin is required when local_proxy.enabled is true "
            "(e.g. https://myapp-dev.localdev.temp.build)"
        )
    hosts = hostnames_from_origins(origins)
    if not hosts:
        raise LocalProxyConfigError(f"Could not derive hostname from site_origin: {origins!r}")
    if len(hosts) > 1:
        raise LocalProxyConfigError(
            "local_proxy requires a single site_origin hostname; "
            f"got {len(hosts)}: {', '.join(hosts)}"
        )
    return hosts[0]


def local_proxy_uses_routes_list(info: dict[str, Any]) -> bool:
    """True when ``local_proxy.routes`` defines explicit host/upstream entries."""
    block = _local_proxy_block(info)
    routes = block.get("routes")
    return isinstance(routes, list) and len(routes) > 0


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


def local_proxy_routes(
    info: dict[str, Any],
    config: ProjectConfig,
    env_name: str,
) -> list[LocalProxyRoute]:
    """Return all proxy routes for this environment (legacy single-route or ``routes`` list)."""
    if not local_proxy_enabled(info):
        return []

    if local_proxy_uses_routes_list(info):
        block = _local_proxy_block(info)
        default_host = local_proxy_hostname(info)
        default_upstream_host = local_proxy_upstream_host(info)
        entries: list[LocalProxyRoute] = []
        for index, raw in enumerate(block["routes"]):
            if not isinstance(raw, dict):
                raise LocalProxyConfigError(
                    f"local_proxy.routes[{index}] must be a mapping (got {type(raw).__name__})"
                )
            host_raw = raw.get("host")
            host = (
                _hostname_from_route_host(host_raw, field=f"local_proxy.routes[{index}].host")
                if host_raw is not None
                else default_host
            )
            port_raw = raw.get("upstream_port") or raw.get("upstreamPort")
            if port_raw is None:
                raise LocalProxyConfigError(
                    f"local_proxy.routes[{index}].upstream_port is required"
                )
            port = _parse_upstream_port(port_raw, field=f"local_proxy.routes[{index}].upstream_port")
            upstream_host_raw = raw.get("upstream_host") or raw.get("upstreamHost")
            upstream_host = (
                str(upstream_host_raw).strip()
                if upstream_host_raw is not None
                else default_upstream_host
            )
            if not upstream_host:
                raise LocalProxyConfigError(
                    f"local_proxy.routes[{index}].upstream_host must not be empty"
                )
            rid = route_id_for_host(config, env_name, host)
            entries.append(
                LocalProxyRoute(
                    route_id=rid,
                    host=host,
                    upstream_dial=f"{upstream_host}:{port}",
                )
            )
        return entries

    return [
        LocalProxyRoute(
            route_id=route_id(config, env_name),
            host=local_proxy_hostname(info),
            upstream_dial=local_proxy_upstream_dial(info),
        )
    ]


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


def build_route_config(route_id_value: str, host: str, upstream_dial: str) -> dict[str, Any]:
    """JSON body for ``PUT /id/<route_id>`` on the Caddy admin API."""
    return {
        "@id": route_id_value,
        "match": [{"host": [host]}],
        "handle": [
            {
                "handler": "reverse_proxy",
                "upstreams": [{"dial": upstream_dial}],
            }
        ],
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


def ensure_proxy_running(*, dry_run: bool = False) -> int:
    """Start ``catalpa-local-proxy`` if it is not already running."""
    if proxy_container_id():
        return 0

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
        return start.returncode

    data_dir = local_proxy_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        LOCAL_PROXY_CONTAINER,
        "--add-host=host.docker.internal:host-gateway",
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
    dry_run: bool = False,
) -> int:
    body = build_route_config(route_id_value, host, upstream_dial)
    if dry_run:
        print(
            f"dry-run: would upsert route /id/{route_id_value} "
            f"host={host!r} upstream={upstream_dial!r}",
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


def _route_context_from_id_and_host(route_id_value: str, host: str) -> str:
    """Best-effort ``project/env`` label from a route ``@id`` and matched host."""
    prefix = f"{_ROUTE_ID_PREFIX}-"
    if not route_id_value.startswith(prefix):
        return route_id_value
    host_suffix = _sanitize_route_label(host, field="host")
    if host_suffix and route_id_value.endswith(f"-{host_suffix}"):
        base = route_id_value[len(prefix) :][: -(len(host_suffix) + 1)]
    else:
        base = route_id_value[len(prefix) :]
    parts = base.rsplit("-", 1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return base or route_id_value


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
        live: list[str] = []
        for route in routes:
            rid = route.get("@id")
            if not isinstance(rid, str) or not rid.startswith(f"{_ROUTE_ID_PREFIX}-"):
                continue
            host = _route_host_from_config(route)
            upstream = _route_upstream_from_config(route)
            context = _route_context_from_id_and_host(rid, host)
            live.append(f"  {host} -> {upstream}  ({context})")
        if live:
            lines.append("live sites:")
            lines.extend(live)
        else:
            lines.append("live sites: (none)")
    except (LocalProxyConfigError, json.JSONDecodeError) as e:
        lines.append(f"live sites: {e}")
    return lines


def print_proxy_status(*, file: TextIO | None = None) -> None:
    out = sys.stderr if file is None else file
    for line in proxy_status_lines():
        print(line, file=out)


def local_proxy_public_url(info: dict[str, Any]) -> str:
    origins = parse_site_origins_from_info(info)
    return origins[0] if origins else f"https://{local_proxy_hostname(info)}"


def print_local_proxy_url(info: dict[str, Any], *, file: TextIO | None = None) -> None:
    out = sys.stderr if file is None else file
    origins = parse_site_origins_from_info(info)
    if origins:
        for origin in origins:
            print(f"Local dev URL: {origin}", file=out)
    elif local_proxy_enabled(info):
        print(f"Local dev URL: https://{local_proxy_hostname(info)}", file=out)


def ca_is_trusted() -> bool:
    """Best-effort check whether Caddy's local CA appears trusted on this machine."""
    import platform

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


def sync_local_proxy_for_compose_action(
    info: dict[str, Any],
    config: ProjectConfig,
    env_name: str,
    compose_args: list[str],
    *,
    dry_run: bool = False,
) -> int:
    """Ensure proxy + route(s) on ``up``; remove route(s) on ``down``."""
    if not local_proxy_enabled(info):
        return 0

    routes = local_proxy_routes(info, config, env_name)
    if not compose_args:
        return 0

    verb = compose_args[0]
    if verb == "up":
        rc = ensure_proxy_running(dry_run=dry_run)
        if rc != 0:
            return rc
        for entry in routes:
            rc = upsert_route(entry.route_id, entry.host, entry.upstream_dial, dry_run=dry_run)
            if rc != 0:
                return rc
        print_local_proxy_url(info)
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
