"""LAN dev access: auto-detect host addresses for phone/tablet testing."""

from __future__ import annotations

import platform
import subprocess
import sys
from typing import Any, TextIO


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


def dev_lan_access_enabled(info: dict[str, Any]) -> bool:
    if info.get("docker_host"):
        return False
    return bool(info.get("dev_lan_access", False))


def dev_lan_port_from_info(info: dict[str, Any]) -> int:
    env = info.get("env") or {}
    if not isinstance(env, dict):
        env = {}
    raw = env.get("node_port") or env.get("NODE_PORT") or "8080"
    try:
        return int(str(raw).strip())
    except ValueError:
        return 8080


def format_dev_lan_urls(info: dict[str, Any], hosts: list[str] | None = None) -> list[str]:
    """Host-side URL list for CLI / VS Code (no Docker required)."""
    if not dev_lan_access_enabled(info):
        return []
    if hosts is None:
        hosts = detect_dev_lan_hosts()
    port = dev_lan_port_from_info(info)
    return [f"http://{host}:{port}" for host in hosts]


def build_dev_lan_env(info: dict[str, Any]) -> dict[str, str]:
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


def print_dev_lan_urls(info: dict[str, Any], *, file: TextIO | None = None) -> list[str]:
    """Print LAN URLs to stderr; return URL list."""
    out = sys.stderr if file is None else file
    urls = format_dev_lan_urls(info)
    if urls:
        print("LAN dev URLs:", file=out)
        for url in urls:
            print(f"  {url}", file=out)
    return urls
