"""DigitalOcean project resolution and project-scoped droplet listing via ``doctl``."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from catalpa_tooling.config import DigitalOceanConfig

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_DROPLET_URN_RE = re.compile(r"^do:droplet:(\d+)$", re.IGNORECASE)

_DEFAULT_COLUMNS = ("ID", "Name", "PublicIPv4", "PrivateIPv4", "Region", "Status")


def _is_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value.strip()))


def _projects_list(*, context: str | None) -> list[dict[str, Any]]:
    from catalpa_tooling.doctl_binary import run_doctl_json

    data = run_doctl_json(["projects", "list"], context=context)
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    return []


def resolve_project_id(
    project: str | None,
    *,
    do_config: DigitalOceanConfig | None,
    context: str | None,
) -> str:
    """Resolve a DigitalOcean project UUID from CLI flag or tooling manifest."""
    effective_context = context or (do_config.context if do_config else None)

    if project:
        candidate = project.strip()
        if _is_uuid(candidate):
            return candidate
        for item in _projects_list(context=effective_context):
            name = str(item.get("name") or item.get("Name") or "")
            if name.lower() == candidate.lower():
                pid = item.get("id") or item.get("ID")
                if pid:
                    return str(pid)
        print(f"No DigitalOcean project named {candidate!r}", file=sys.stderr)
        raise SystemExit(1)

    if do_config and do_config.project_id:
        return do_config.project_id
    if do_config and do_config.project_name:
        return resolve_project_id(do_config.project_name, do_config=None, context=effective_context)

    print(
        "Pass --project or set digitalocean.project_name / project_id in tooling.yaml",
        file=sys.stderr,
    )
    raise SystemExit(1)


def droplet_ids_from_resource_urns(urns: Sequence[str]) -> list[int]:
    ids: list[int] = []
    for urn in urns:
        m = _DROPLET_URN_RE.match(urn.strip())
        if m:
            ids.append(int(m.group(1)))
    return ids


def list_project_resource_urns(
    project_id: str,
    *,
    context: str | None,
) -> list[str]:
    from catalpa_tooling.doctl_binary import run_doctl_json

    data = run_doctl_json(["projects", "resources", "list", project_id], context=context)
    urns: list[str] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str):
                urns.append(item)
            elif isinstance(item, dict):
                urn = item.get("urn") or item.get("URN")
                if urn:
                    urns.append(str(urn))
    return urns


def _public_ipv4(networks: dict[str, Any] | None) -> str:
    if not networks:
        return ""
    v4 = networks.get("v4") or []
    if not isinstance(v4, list):
        return ""
    for entry in v4:
        if isinstance(entry, dict) and entry.get("type") == "public":
            return str(entry.get("ip_address") or "")
    return ""


def _private_ipv4(networks: dict[str, Any] | None) -> str:
    if not networks:
        return ""
    v4 = networks.get("v4") or []
    if not isinstance(v4, list):
        return ""
    for entry in v4:
        if isinstance(entry, dict) and entry.get("type") == "private":
            return str(entry.get("ip_address") or "")
    return ""


def _region_slug(droplet: dict[str, Any]) -> str:
    region = droplet.get("region")
    if isinstance(region, dict):
        return str(region.get("slug") or region.get("name") or "")
    return str(region or "")


def _droplet_row(droplet: dict[str, Any], columns: tuple[str, ...]) -> list[str]:
    networks = droplet.get("networks") if isinstance(droplet.get("networks"), dict) else {}
    values: dict[str, str] = {
        "ID": str(droplet.get("id", "")),
        "Name": str(droplet.get("name", "")),
        "PublicIPv4": _public_ipv4(networks),
        "PrivateIPv4": _private_ipv4(networks),
        "Region": _region_slug(droplet),
        "Status": str(droplet.get("status", "")),
    }
    return [values.get(col, "") for col in columns]


def _print_droplet_table(droplets: list[dict[str, Any]], columns: tuple[str, ...]) -> None:
    widths = [len(c) for c in columns]
    rows: list[list[str]] = []
    for d in droplets:
        row = _droplet_row(d, columns)
        rows.append(row)
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    print(header)
    for row in rows:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(columns))))


def list_project_droplets(
    project_id: str,
    *,
    context: str | None,
    columns: tuple[str, ...] | None = None,
    as_json: bool = False,
) -> int:
    from catalpa_tooling.doctl_binary import run_doctl_json

    urns = list_project_resource_urns(project_id, context=context)
    droplet_ids = droplet_ids_from_resource_urns(urns)
    if not droplet_ids:
        print("No droplets in this project.")
        return 0

    all_droplets = run_doctl_json(["compute", "droplet", "list"], context=context)
    if not isinstance(all_droplets, list):
        all_droplets = []

    id_set = set(droplet_ids)
    matched = [
        d for d in all_droplets if isinstance(d, dict) and int(d.get("id", -1)) in id_set
    ]
    matched.sort(key=lambda d: int(d.get("id", 0)))

    if as_json:
        print(json.dumps(matched, indent=2))
        return 0

    cols = columns or _DEFAULT_COLUMNS
    _print_droplet_table(matched, cols)
    return 0
