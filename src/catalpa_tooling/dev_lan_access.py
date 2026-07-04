"""LAN dev access: reach local dev stacks from phones/tablets on the same network."""

from __future__ import annotations

import platform
import re
import subprocess
import sys
from typing import TYPE_CHECKING, Any, TextIO
from urllib.parse import urlparse

from catalpa_tooling.site_origin import hostnames_from_origins, parse_site_origins_from_info

if TYPE_CHECKING:
    from catalpa_tooling.config import ProjectConfig

from catalpa_tooling.config import DEFAULT_DEV_LAN_DNS_SUFFIX, DEFAULT_DEV_SITE_ORIGIN_BASE

DEFAULT_LAN_DNS_SUFFIX = DEFAULT_DEV_LAN_DNS_SUFFIX
DEFAULT_SITE_ORIGIN_BASE = DEFAULT_DEV_SITE_ORIGIN_BASE
LOCAL_PROXY_CA_HTTP_PATH = "/catalpa-local-ca.crt"
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def _run_text(cmd: list[str]) -> str:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _is_valid_lan_host(host: str) -> bool:
    if not host or host in ("127.0.0.1", "localhost", "::1"):
        return False
    if host.startswith("169.254."):
        return False
    return True


def _macos_lan_hosts() -> list[str]:
    hosts: list[str] = []
    for iface in ("en0", "en1"):
        ip = _run_text(["ipconfig", "getifaddr", iface])
        if ip and _is_valid_lan_host(ip):
            hosts.append(ip)
    bonjour = _run_text(["scutil", "--get", "LocalHostName"])
    if bonjour:
        local = f"{bonjour}.local"
        if _is_valid_lan_host(local):
            hosts.append(local)
    return hosts


def _linux_lan_hosts() -> list[str]:
    hosts: list[str] = []
    route = _run_text(["ip", "route", "get", "1.1.1.1"])
    if route:
        parts = route.split()
        if "dev" in parts:
            idx = parts.index("dev")
            if idx + 1 < len(parts):
                iface = parts[idx + 1]
                addr_out = _run_text(["ip", "-4", "addr", "show", iface])
                for line in addr_out.splitlines():
                    line = line.strip()
                    if line.startswith("inet "):
                        addr = line.split()[1].split("/")[0]
                        if _is_valid_lan_host(addr):
                            hosts.append(addr)
    if not hosts:
        for ip in _run_text(["hostname", "-I"]).split():
            ip = ip.strip()
            if _is_valid_lan_host(ip):
                hosts.append(ip)
    return hosts


def detect_dev_lan_hosts() -> list[str]:
    """Return LAN-reachable host identifiers (IPv4 and Bonjour ``.local`` on macOS)."""
    system = platform.system()
    if system == "Darwin":
        candidates = _macos_lan_hosts()
    elif system == "Linux":
        candidates = _linux_lan_hosts()
    else:
        candidates = []
    seen: set[str] = set()
    out: list[str] = []
    for host in candidates:
        key = host.lower()
        if key not in seen:
            seen.add(key)
            out.append(host)
    return out


def detect_dev_lan_ipv4() -> list[str]:
    """IPv4 addresses only (required for magic-DNS hostnames)."""
    return [h for h in detect_dev_lan_hosts() if _IPV4_RE.match(h)]


def ip_to_dns_label(ip: str) -> str:
    """``192.168.1.42`` -> ``192-168-1-42`` for sslip.io / nip.io style names."""
    return ip.strip().replace(".", "-")


def _is_remote_docker_host(docker_host: object) -> bool:
    return str(docker_host or "").strip().startswith("ssh://")


def _local_proxy_block(info: dict[str, Any]) -> dict[str, Any]:
    raw = info.get("local_proxy")
    if raw is None or raw is False:
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def local_proxy_enabled_for_lan(info: dict[str, Any]) -> bool:
    if _is_remote_docker_host(info.get("docker_host")):
        return False
    return bool(_local_proxy_block(info).get("enabled", False))


def lan_access_enabled(info: dict[str, Any]) -> bool:
    """True when this env should register LAN routes on the machine-wide dev proxy."""
    if not local_proxy_enabled_for_lan(info):
        return False
    block = _local_proxy_block(info)
    return bool(block.get("lan_access") or info.get("dev_lan_access"))


def dev_lan_access_enabled(info: dict[str, Any]) -> bool:
    """Backward-compatible alias: LAN via dev proxy, or legacy flag without proxy check."""
    if lan_access_enabled(info):
        return True
    if _is_remote_docker_host(info.get("docker_host")):
        return False
    return bool(info.get("dev_lan_access", False))


def _site_origin_base(config: "ProjectConfig | None" = None) -> str:
    if config is not None:
        return config.dev.site_origin_base
    return DEFAULT_SITE_ORIGIN_BASE


def lan_dns_suffix_from_info(
    info: dict[str, Any],
    *,
    config: "ProjectConfig | None" = None,
) -> str:
    block = _local_proxy_block(info)
    raw = block.get("lan_dns_suffix") or block.get("lanDnsSuffix")
    if raw is not None and str(raw).strip():
        return str(raw).strip().rstrip(".")
    if config is not None:
        return config.dev.lan_dns_suffix
    return DEFAULT_LAN_DNS_SUFFIX


def lan_hostname_for(
    site_host: str,
    ip: str,
    *,
    base: str | None = None,
    lan_dns_suffix: str | None = None,
    config: "ProjectConfig | None" = None,
) -> str:
    """Build a magic-DNS hostname that resolves to ``ip`` from any device."""
    host = site_host.strip().rstrip(".")
    ip_label = ip_to_dns_label(ip)
    suffix = (lan_dns_suffix or lan_dns_suffix_from_info({}, config=config)).strip().rstrip(".")
    origin_base = base if base is not None else _site_origin_base(config)
    base_suffix = f".{origin_base}"
    if host.endswith(base_suffix):
        prefix = host[: -len(base_suffix)]
        if prefix:
            return f"{prefix}.{ip_label}.{suffix}"
    label = host.split(".")[0] if host else "app"
    return f"{label}.{ip_label}.{suffix}"


def collect_lan_site_hosts(info: dict[str, Any]) -> list[str]:
    """Hostnames from ``site_origin`` plus explicit ``local_proxy.routes[].host`` entries."""
    hosts: list[str] = []
    seen: set[str] = set()
    for origin in parse_site_origins_from_info(info):
        for host in hostnames_from_origins([origin]):
            if host not in seen:
                seen.add(host)
                hosts.append(host)
    block = _local_proxy_block(info)
    routes = block.get("routes")
    if isinstance(routes, list):
        for raw in routes:
            if not isinstance(raw, dict):
                continue
            host_raw = raw.get("host")
            if host_raw is None:
                continue
            host = str(host_raw).strip()
            if "://" in host:
                parsed = urlparse(host if host.startswith("http") else f"https://{host}")
                host = parsed.hostname or host.split("/")[0]
            else:
                host = host.split("/")[0]
            if host and host not in seen:
                seen.add(host)
                hosts.append(host)
    return hosts


def format_proxy_lan_urls(
    info: dict[str, Any],
    site_hosts: list[str] | None = None,
    *,
    ips: list[str] | None = None,
    config: "ProjectConfig | None" = None,
) -> list[str]:
    """HTTPS URLs reachable from LAN devices via the dev proxy."""
    if not lan_access_enabled(info):
        return []
    if site_hosts is None:
        site_hosts = collect_lan_site_hosts(info)
    if not site_hosts:
        return []
    if ips is None:
        ips = detect_dev_lan_ipv4()
    if not ips:
        return []
    suffix = lan_dns_suffix_from_info(info, config=config)
    urls: list[str] = []
    seen: set[str] = set()
    for site_host in site_hosts:
        for ip in ips:
            lan_host = lan_hostname_for(
                site_host, ip, lan_dns_suffix=suffix, config=config
            )
            url = f"https://{lan_host}"
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def ca_download_url_for_ip(ip: str, info: dict[str, Any] | None = None) -> str:
    """Plain-HTTP URL to download the local dev CA root (for one-time device trust)."""
    suffix = lan_dns_suffix_from_info(info or {})
    host = f"{ip_to_dns_label(ip)}.{suffix}"
    return f"http://{host}{LOCAL_PROXY_CA_HTTP_PATH}"


def build_proxy_lan_env(
    info: dict[str, Any],
    site_hosts: list[str] | None = None,
    *,
    config: "ProjectConfig | None" = None,
) -> dict[str, str]:
    """Env vars for Django / frontend when LAN access via the dev proxy is enabled."""
    if not lan_access_enabled(info):
        return {}
    if site_hosts is None:
        site_hosts = collect_lan_site_hosts(info)
    urls = format_proxy_lan_urls(info, site_hosts, config=config)
    if not urls:
        return {}
    out: dict[str, str] = {}
    # Django settings accept https:// origins in DOMAIN for ALLOWED_HOSTS + CSRF.
    out["DOMAIN"] = ", ".join(urls)
    suffix = lan_dns_suffix_from_info(info, config=config)
    origin_base = _site_origin_base(config)
    if not suffix.endswith(origin_base):
        out["VITE_EXTRA_ALLOWED_HOSTS"] = f".{suffix}"
    return out


def dev_lan_port_from_info(info: dict[str, Any]) -> int:
    """Legacy host-port LAN URLs (unused when ``local_proxy`` + LAN access is enabled)."""
    env = info.get("env") or {}
    if not isinstance(env, dict):
        env = {}
    block = _local_proxy_block(info)
    raw = (
        block.get("upstream_port")
        or block.get("upstreamPort")
        or env.get("node_port")
        or env.get("NODE_PORT")
        or "8080"
    )
    try:
        return int(str(raw).strip())
    except ValueError:
        return 8080


def format_dev_lan_urls(info: dict[str, Any], hosts: list[str] | None = None) -> list[str]:
    """URL list for CLI / VS Code."""
    if lan_access_enabled(info):
        return format_proxy_lan_urls(info)
    if not dev_lan_access_enabled(info):
        return []
    if hosts is None:
        hosts = detect_dev_lan_hosts()
    port = dev_lan_port_from_info(info)
    return [f"http://{host}:{port}" for host in hosts]


def build_dev_lan_env(
    info: dict[str, Any],
    *,
    config: "ProjectConfig | None" = None,
) -> dict[str, str]:
    if lan_access_enabled(info):
        return build_proxy_lan_env(info, config=config)
    if not dev_lan_access_enabled(info):
        return {}
    hosts = detect_dev_lan_hosts()
    if not hosts:
        return {}
    origins = format_dev_lan_urls(info, hosts=hosts)
    return {
        "BERO_EXTRA_ALLOWED_HOSTS": ",".join(hosts),
        "BERO_EXTRA_ORIGINS": ",".join(origins),
    }


def print_proxy_lan_urls(
    info: dict[str, Any],
    site_hosts: list[str] | None = None,
    *,
    file: TextIO | None = None,
) -> list[str]:
    out = sys.stderr if file is None else file
    urls = format_proxy_lan_urls(info, site_hosts)
    if not urls:
        return []
    print("LAN dev URLs (trust CA once: `dk proxy ca`):", file=out)
    for url in urls:
        print(f"  {url}", file=out)
    ips = detect_dev_lan_ipv4()
    if ips:
        ca_url = ca_download_url_for_ip(ips[0], info)
        print(f"  CA install: {ca_url}", file=out)
    return urls


def print_dev_lan_urls(info: dict[str, Any], *, file: TextIO | None = None) -> list[str]:
    """Print LAN URLs to stderr; return URL list."""
    if lan_access_enabled(info):
        return print_proxy_lan_urls(info, file=file)
    out = sys.stderr if file is None else file
    urls = format_dev_lan_urls(info)
    if urls:
        print("LAN dev URLs:", file=out)
        for url in urls:
            print(f"  {url}", file=out)
    return urls
