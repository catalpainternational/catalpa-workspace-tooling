"""Public DNS resolution checks for ``dk <env> host`` (stdlib only, no dig)."""

from __future__ import annotations

import ipaddress
import socket
import sys
from typing import Any
from urllib.parse import urlparse

from catalpa_tooling.site_origin import (
    hostnames_from_origins,
    parse_site_origins_from_info,
)

DEFAULT_SSH_USER = "root"


def _strip_port(host: str) -> str:
    h = host.strip()
    if not h:
        return ""
    if ":" in h and not h.startswith("["):
        return h.split(":", 1)[0]
    return h


def resolve_ipv4(host: str) -> list[str]:
    """Resolve ``host`` to IPv4 addresses via the system resolver."""
    name = _strip_port(host)
    if not name:
        return []
    try:
        infos = socket.getaddrinfo(
            name,
            None,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve {name!r}: {e}") from e
    seen: set[str] = set()
    out: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def _docker_host_hostname(docker_host: str) -> str:
    """Return hostname or IP from ``docker_host`` (no user, no port)."""
    raw = (docker_host or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme and parsed.scheme != "ssh":
            return ""
        host = parsed.hostname or ""
        if not host and parsed.path:
            path = parsed.path.lstrip("/")
            if "@" in path:
                host = path.split("@", 1)[1]
            else:
                host = path
        return _strip_port(host)
    if "@" in raw:
        return _strip_port(raw.split("@", 1)[1])
    return _strip_port(raw)


def docker_host_expected_ipv4(docker_host: str) -> str:
    """Return a single expected IPv4 from ``docker_host`` for DNS comparison."""
    host = _docker_host_hostname(docker_host)
    if not host:
        raise ValueError("docker_host is empty or unparseable")
    try:
        ipaddress.IPv4Address(host)
        return host
    except ipaddress.AddressValueError:
        pass
    addrs = resolve_ipv4(host)
    if not addrs:
        raise ValueError(f"docker_host host {host!r} did not resolve to any IPv4 address")
    if len(addrs) > 1:
        raise ValueError(
            f"docker_host host {host!r} resolved to multiple IPv4 addresses ({', '.join(addrs)}); "
            "use a literal IPv4 in docker_host"
        )
    return addrs[0]


def verify_public_dns(hostnames: list[str], expected_ip: str) -> int:
    """Verify each hostname resolves to ``expected_ip``. Returns exit code."""
    ip = expected_ip.strip()
    if not ip:
        print("Cannot verify public DNS: expected IP is empty.", file=sys.stderr)
        return 1

    failures: list[str] = []
    ok_count = 0

    for raw in hostnames:
        host = _strip_port(raw)
        if not host:
            continue
        try:
            resolved = resolve_ipv4(host)
        except ValueError as e:
            failures.append(str(e))
            continue
        if ip in resolved:
            ok_count += 1
        else:
            actual = ", ".join(resolved) if resolved else "(no A records)"
            failures.append(
                f"Public DNS mismatch for {host!r}: expected {ip!r}, got {actual!r}"
            )

    if failures:
        for msg in failures:
            print(msg, file=sys.stderr)
        return 1

    if ok_count:
        print(
            f"Public DNS OK: {ok_count} hostname(s) resolve to {ip}.",
            file=sys.stderr,
        )
    return 0


def verify_public_dns_from_info(info: dict[str, Any], expected_ip: str) -> int:
    """Verify ``site_origin`` hostnames resolve to ``expected_ip`` via public DNS."""
    origins = parse_site_origins_from_info(info)
    if not origins:
        return 0
    hostnames = hostnames_from_origins(origins)
    if not hostnames:
        return 0
    return verify_public_dns(hostnames, expected_ip)
