"""DigitalOcean DNS verification and sync for ``dk <env> host``."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.site_origin import hostnames_from_origins, parse_site_origins_from_info

DEFAULT_DNS_TTL = 300
MIN_DNS_TTL = 30
MAX_DNS_TTL = 86400
_DOMAIN_URN_RE = re.compile(r"^do:domain:(.+)$", re.IGNORECASE)


class DnsTtlConfigError(ValueError):
    """Invalid ``digitalocean.dns_ttl`` in env ``info.yaml``."""


def env_dns_ttl(info: dict[str, Any]) -> int:
    """TTL for DO A records from ``digitalocean.dns_ttl`` in info.yaml (default 300)."""
    raw = info.get("digitalocean")
    if not isinstance(raw, dict):
        return DEFAULT_DNS_TTL
    value = raw.get("dns_ttl")
    if value is None:
        return DEFAULT_DNS_TTL
    if isinstance(value, bool) or not isinstance(value, int):
        raise DnsTtlConfigError(
            f"digitalocean.dns_ttl must be an integer (seconds), got {value!r}"
        )
    if not MIN_DNS_TTL <= value <= MAX_DNS_TTL:
        raise DnsTtlConfigError(
            f"digitalocean.dns_ttl must be between {MIN_DNS_TTL} and {MAX_DNS_TTL}, got {value}"
        )
    return value


@dataclass(frozen=True)
class HostDnsTarget:
    """One ``site_origin`` hostname mapped to a DO DNS zone and record name."""

    hostname: str
    zone: str
    record_name: str


@dataclass(frozen=True)
class DnsVerifyResult:
    """Outcome of checking one hostname's DO DNS (A or CNAME chain)."""

    target: HostDnsTarget
    ok: bool
    message: str


def _strip_port(hostname: str) -> str:
    host = hostname.strip().lower()
    if not host:
        return ""
    if ":" in host and not host.startswith("["):
        host = host.split(":", 1)[0]
    return host


def hostname_to_zone_and_record_name(hostname: str, registered_zones: list[str]) -> HostDnsTarget | None:
    """Map ``hostname`` to DO zone + relative record name using longest zone suffix match."""
    host = _strip_port(hostname)
    if not host:
        return None
    zones = sorted(
        {_strip_port(z) for z in registered_zones if _strip_port(z)},
        key=len,
        reverse=True,
    )
    for zone in zones:
        if host == zone:
            # DO apex records use ``@``; the zone name as ``--record-name`` creates
            # ``zone.zone`` (e.g. khs.temp.build.khs.temp.build).
            return HostDnsTarget(hostname=host, zone=zone, record_name="@")
        suffix = f".{zone}"
        if host.endswith(suffix):
            label = host[: -len(suffix)]
            if label and "." not in label:
                return HostDnsTarget(hostname=host, zone=zone, record_name=label)
    return None


def targets_from_site_origins(
    hostnames: list[str],
    registered_zones: list[str],
) -> tuple[list[HostDnsTarget], list[str]]:
    """Return DO-managed targets and hostnames with no matching zone."""
    targets: list[HostDnsTarget] = []
    skipped: list[str] = []
    for raw in hostnames:
        host = _strip_port(raw)
        if not host:
            continue
        mapped = hostname_to_zone_and_record_name(host, registered_zones)
        if mapped is None:
            skipped.append(host)
        else:
            targets.append(mapped)
    return targets, skipped


def list_registered_domains(*, context: str | None) -> list[str]:
    """Return domain names registered with DigitalOcean DNS."""
    from catalpa_tooling.doctl_binary import run_doctl_json

    data = run_doctl_json(["compute", "domain", "list"], context=context)
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for item in data:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def list_domain_records(zone: str, *, context: str | None) -> list[dict[str, Any]]:
    from catalpa_tooling.doctl_binary import run_doctl_json

    data = run_doctl_json(
        ["compute", "domain", "records", "list", zone],
        context=context,
    )
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _record_name_matches(record: dict[str, Any], expected: str, zone: str) -> bool:
    name = str(record.get("name") or "").strip().lower()
    expected_l = expected.strip().lower()
    zone_l = zone.strip().lower()
    if name == expected_l:
        return True
    if expected_l == zone_l and name in ("@", zone_l):
        return True
    return False


def _normalize_rdata(data: str) -> str:
    return data.strip().rstrip(".").lower()


def _cname_target_hostname(rdata: str, zone: str) -> str:
    """Turn DO CNAME rdata (e.g. ``@``, ``catalpa.io.``) into a hostname to resolve."""
    target = _normalize_rdata(rdata)
    if not target or target == "@":
        return _strip_port(zone)
    return target


def find_dns_record(
    zone: str,
    record_name: str,
    record_type: str,
    *,
    context: str | None,
) -> dict[str, Any] | None:
    """Return the first record of ``record_type`` matching ``record_name`` in ``zone``."""
    want = record_type.strip().upper()
    for record in list_domain_records(zone, context=context):
        if str(record.get("type") or "").upper() != want:
            continue
        if _record_name_matches(record, record_name, zone):
            return record
    return None


def find_a_record(
    zone: str,
    record_name: str,
    *,
    context: str | None,
) -> dict[str, Any] | None:
    """Return the first A record matching ``record_name`` in ``zone``."""
    return find_dns_record(zone, record_name, "A", context=context)


def find_a_record_exact(
    zone: str,
    record_name: str,
    *,
    context: str | None,
) -> dict[str, Any] | None:
    """Return an A record whose name exactly matches ``record_name`` (no apex aliases)."""
    expected = record_name.strip().lower()
    for record in list_domain_records(zone, context=context):
        if str(record.get("type") or "").upper() != "A":
            continue
        name = str(record.get("name") or "").strip().lower()
        if name == expected:
            return record
    return None


def find_cname_record(
    zone: str,
    record_name: str,
    *,
    context: str | None,
) -> dict[str, Any] | None:
    """Return the first CNAME record matching ``record_name`` in ``zone``."""
    return find_dns_record(zone, record_name, "CNAME", context=context)


def do_dns_resolve_ipv4(
    hostname: str,
    registered_zones: list[str],
    *,
    context: str | None,
    visited: frozenset[str] | None = None,
    max_depth: int = 8,
) -> str | None:
    """Follow DO A/CNAME records for ``hostname`` and return an IPv4, or None."""
    host = _strip_port(hostname)
    if not host:
        return None
    seen = visited or frozenset()
    if host in seen or len(seen) >= max_depth:
        return None
    seen = seen | {host}

    mapped = hostname_to_zone_and_record_name(host, registered_zones)
    if mapped is None:
        return None

    a_record = find_a_record(mapped.zone, mapped.record_name, context=context)
    if a_record is not None:
        return str(a_record.get("data") or "").strip() or None

    cname = find_cname_record(mapped.zone, mapped.record_name, context=context)
    if cname is None:
        return None

    target_host = _cname_target_hostname(str(cname.get("data") or ""), mapped.zone)
    return do_dns_resolve_ipv4(
        target_host,
        registered_zones,
        context=context,
        visited=seen,
        max_depth=max_depth,
    )


def _verify_one_dns_record(
    target: HostDnsTarget,
    expected_ip: str,
    *,
    context: str | None,
    registered_zones: list[str],
) -> DnsVerifyResult:
    ip = expected_ip.strip()
    record = find_a_record(target.zone, target.record_name, context=context)
    if record is not None:
        actual = str(record.get("data") or "").strip()
        if actual != ip:
            return DnsVerifyResult(
                target=target,
                ok=False,
                message=(
                    f"A record for {target.hostname!r} points to {actual!r}, "
                    f"expected droplet {ip!r}"
                ),
            )
        return DnsVerifyResult(
            target=target,
            ok=True,
            message=f"{target.hostname!r} -> {ip}",
        )

    resolved = do_dns_resolve_ipv4(
        target.hostname, registered_zones, context=context
    )
    if resolved == ip:
        cname = find_cname_record(target.zone, target.record_name, context=context)
        if cname is not None:
            cname_target = _cname_target_hostname(str(cname.get("data") or ""), target.zone)
            return DnsVerifyResult(
                target=target,
                ok=True,
                message=f"{target.hostname!r} CNAME -> {cname_target} -> {ip}",
            )
        return DnsVerifyResult(
            target=target,
            ok=True,
            message=f"{target.hostname!r} -> {ip}",
        )

    cname = find_cname_record(target.zone, target.record_name, context=context)
    if cname is not None:
        cname_target = _cname_target_hostname(str(cname.get("data") or ""), target.zone)
        if resolved:
            return DnsVerifyResult(
                target=target,
                ok=False,
                message=(
                    f"CNAME for {target.hostname!r} points to {cname_target!r}, "
                    f"which resolves to {resolved!r} (expected droplet {ip!r})"
                ),
            )
        return DnsVerifyResult(
            target=target,
            ok=False,
            message=(
                f"CNAME for {target.hostname!r} points to {cname_target!r}, "
                f"but no A record on DO DNS resolves to droplet {ip!r}"
            ),
        )

    return DnsVerifyResult(
        target=target,
        ok=False,
        message=(
            f"no A or CNAME record for {target.hostname!r} in zone {target.zone!r}"
        ),
    )


def verify_host_dns(
    config: ProjectConfig,
    info: dict[str, Any],
    *,
    droplet_ip: str,
    context: str | None,
    project_id: str | None = None,
) -> int:
    """Verify DO DNS zones and A/CNAME records for ``site_origin`` hostnames. Returns exit code."""
    from catalpa_tooling.doctl_projects import (
        list_project_domain_urns,
        resolve_project_id,
    )

    origins = parse_site_origins_from_info(info)
    if not origins:
        return 0

    hostnames = hostnames_from_origins(origins)
    if not hostnames:
        return 0

    registered = list_registered_domains(context=context)
    targets, skipped = targets_from_site_origins(hostnames, registered)

    for host in skipped:
        print(
            f"Note: {host!r} is not under a DigitalOcean-managed domain; skipping DNS check.",
            file=sys.stderr,
        )

    if not targets:
        return 0

    do_config = config.digitalocean
    effective_project_id: str | None = project_id
    if effective_project_id is None and do_config and (
        do_config.project_id or do_config.project_name
    ):
        try:
            effective_project_id = resolve_project_id(None, do_config=do_config, context=context)
        except SystemExit:
            return 1

    project_domains: set[str] = set()
    if effective_project_id:
        project_domains = list_project_domain_urns(effective_project_id, context=context)

    zones_seen: set[str] = set()
    failures: list[str] = []
    ok_count = 0

    for target in targets:
        if target.zone in zones_seen:
            pass
        else:
            zones_seen.add(target.zone)
            if effective_project_id and target.zone.lower() not in project_domains:
                failures.append(
                    f"Domain {target.zone!r} is not assigned to the configured DigitalOcean "
                    f"project. Assign it, e.g.:\n"
                    f"  doctl projects resources assign {effective_project_id} "
                    f"--resource=do:domain:{target.zone}"
                )

        result = _verify_one_dns_record(
            target,
            droplet_ip,
            context=context,
            registered_zones=registered,
        )
        if result.ok:
            ok_count += 1
        else:
            failures.append(result.message)

    if failures:
        for msg in failures:
            print(msg, file=sys.stderr)
        return 1

    if ok_count:
        print(
            f"DNS OK: {ok_count} hostname(s) match droplet {droplet_ip} "
            f"across {len(zones_seen)} zone(s).",
            file=sys.stderr,
        )
    return 0


def _delete_domain_record(
    zone: str,
    record_id: object,
    *,
    context: str | None,
    dry_run: bool,
) -> None:
    from catalpa_tooling.doctl_binary import DoctlCommandError, format_doctl_failure, run_doctl

    cmd = [
        "compute",
        "domain",
        "records",
        "delete",
        zone,
        str(record_id),
        "--force",
    ]
    if dry_run:
        print(f"dry-run: would run doctl {' '.join(cmd)}", file=sys.stderr)
        return
    result = run_doctl(cmd, context=context)
    if result.returncode != 0:
        raise DoctlCommandError(format_doctl_failure(result), returncode=result.returncode)


def _remove_malformed_apex_a_record(
    target: HostDnsTarget,
    *,
    context: str | None,
    dry_run: bool,
) -> None:
    """Delete legacy apex A records created with the zone name instead of ``@``."""
    if target.record_name != "@" or target.hostname != target.zone:
        return
    malformed = find_a_record_exact(target.zone, target.zone, context=context)
    if malformed is None:
        return
    record_id = malformed.get("id")
    if record_id is None:
        return
    print(
        f"Removing malformed apex A record {target.zone!r} in zone {target.zone!r} "
        f"(use @ for apex; was {malformed.get('data')!r})",
        file=sys.stderr,
    )
    _delete_domain_record(target.zone, record_id, context=context, dry_run=dry_run)


def _upsert_a_record(
    target: HostDnsTarget,
    ip: str,
    *,
    context: str | None,
    dry_run: bool,
    ttl: int = DEFAULT_DNS_TTL,
) -> None:
    from catalpa_tooling.doctl_binary import run_doctl

    _remove_malformed_apex_a_record(target, context=context, dry_run=dry_run)

    if find_cname_record(target.zone, target.record_name, context=context) is not None:
        print(
            f"DNS unchanged: {target.hostname!r} uses CNAME (not replacing with A).",
            file=sys.stderr,
        )
        return

    existing = find_a_record(target.zone, target.record_name, context=context)
    if existing is None:
        cmd = [
            "compute",
            "domain",
            "records",
            "create",
            target.zone,
            "--record-type",
            "A",
            "--record-name",
            target.record_name,
            "--record-data",
            ip,
            "--record-ttl",
            str(ttl),
        ]
        if dry_run:
            print(
                f"dry-run: would run doctl {' '.join(cmd)}",
                file=sys.stderr,
            )
            return
        result = run_doctl(cmd, context=context)
        if result.returncode != 0:
            from catalpa_tooling.doctl_binary import DoctlCommandError, format_doctl_failure

            raise DoctlCommandError(format_doctl_failure(result), returncode=result.returncode)
        print(f"Created A record {target.hostname!r} -> {ip}", file=sys.stderr)
        return

    record_id = existing.get("id")
    actual = str(existing.get("data") or "").strip()
    existing_ttl_raw = existing.get("ttl")
    try:
        existing_ttl = int(existing_ttl_raw) if existing_ttl_raw is not None else None
    except (TypeError, ValueError):
        existing_ttl = None
    if actual == ip and existing_ttl == ttl:
        print(f"DNS unchanged: {target.hostname!r} -> {ip}", file=sys.stderr)
        return

    cmd = [
        "compute",
        "domain",
        "records",
        "update",
        target.zone,
        "--record-id",
        str(record_id),
        "--record-type",
        "A",
        "--record-name",
        target.record_name,
        "--record-data",
        ip,
        "--record-ttl",
        str(ttl),
    ]
    if dry_run:
        print(
            f"dry-run: would run doctl {' '.join(cmd)} "
            f"(was {actual!r})",
            file=sys.stderr,
        )
        return
    result = run_doctl(cmd, context=context)
    if result.returncode != 0:
        from catalpa_tooling.doctl_binary import DoctlCommandError, format_doctl_failure

        raise DoctlCommandError(format_doctl_failure(result), returncode=result.returncode)
    if actual == ip:
        print(
            f"Updated A record TTL for {target.hostname!r}: {existing_ttl} -> {ttl}",
            file=sys.stderr,
        )
    else:
        print(
            f"Updated A record {target.hostname!r}: {actual!r} -> {ip}",
            file=sys.stderr,
        )


def sync_host_dns(
    config: ProjectConfig,
    info: dict[str, Any],
    *,
    droplet_ip: str,
    context: str | None,
    dry_run: bool = False,
) -> int:
    """Create or update A records on DO-managed zones for ``site_origin`` hostnames."""
    origins = parse_site_origins_from_info(info)
    if not origins:
        return 0

    hostnames = hostnames_from_origins(origins)
    if not hostnames:
        return 0

    registered = list_registered_domains(context=context)
    targets, skipped = targets_from_site_origins(hostnames, registered)

    for host in skipped:
        print(
            f"Note: {host!r} is not under a DigitalOcean-managed domain; skipping DNS sync.",
            file=sys.stderr,
        )

    if not targets:
        return 0

    ip = droplet_ip.strip()
    if not ip:
        print("Cannot sync DNS: droplet has no public IPv4.", file=sys.stderr)
        return 1

    try:
        ttl = env_dns_ttl(info)
    except DnsTtlConfigError as e:
        print(str(e), file=sys.stderr)
        return 1

    try:
        for target in targets:
            _upsert_a_record(target, ip, context=context, dry_run=dry_run, ttl=ttl)
    except Exception as e:
        from catalpa_tooling.doctl_binary import DoctlCommandError

        if isinstance(e, DoctlCommandError):
            print(str(e), file=sys.stderr)
            return e.returncode
        raise

    print(
        f"DNS sync complete: {len(targets)} hostname(s) on DO-managed zone(s).",
        file=sys.stderr,
    )
    return 0

