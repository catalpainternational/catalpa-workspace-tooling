"""DigitalOcean project resolution and project-scoped droplet listing via ``doctl``."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from typing import Any

from catalpa_tooling.config import DigitalOceanConfig, ProjectConfig
from catalpa_tooling.deploy_do_link import (
    droplet_region_slug,
    private_ipv4,
    public_ipv4,
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_DROPLET_URN_RE = re.compile(r"^do:droplet:(\d+)$", re.IGNORECASE)
_DOMAIN_URN_RE = re.compile(r"^do:domain:(.+)$", re.IGNORECASE)

_DEFAULT_COLUMNS = ("ID", "Name", "PublicIPv4", "PrivateIPv4", "Region", "Status")


def _is_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value.strip()))


def _projects_list(*, context: str | None) -> list[dict[str, Any]]:
    from catalpa_tooling.doctl_binary import run_doctl_json

    data = run_doctl_json(["projects", "list"], context=context)
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    return []


def resolve_project_id_dry_run(
    project: str | None,
    *,
    do_config: DigitalOceanConfig | None,
) -> str:
    """Placeholder project UUID for ``droplets create --dry-run`` (no host ``doctl``)."""
    if project:
        candidate = project.strip()
        if _is_uuid(candidate):
            return candidate
        return f"<project:{candidate}>"
    if do_config and do_config.project_id:
        return do_config.project_id
    if do_config and do_config.project_name:
        return f"<project:{do_config.project_name}>"
    return "<project-id>"


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


def domain_names_from_resource_urns(urns: Sequence[str]) -> list[str]:
    """Extract domain names from project resource URNs ``do:domain:<name>``."""
    names: list[str] = []
    for urn in urns:
        m = _DOMAIN_URN_RE.match(urn.strip())
        if m:
            names.append(m.group(1))
    return names


def list_project_domain_urns(
    project_id: str,
    *,
    context: str | None,
) -> set[str]:
    """Return domain names (lowercase) assigned to a DigitalOcean project."""
    urns = list_project_resource_urns(project_id, context=context)
    return {name.strip().lower() for name in domain_names_from_resource_urns(urns)}


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


def _droplet_row(
    droplet: dict[str, Any],
    columns: tuple[str, ...],
    *,
    env_by_droplet_name: dict[str, str] | None = None,
) -> list[str]:
    name = str(droplet.get("name", ""))
    env_label = ""
    if env_by_droplet_name is not None:
        env_label = env_by_droplet_name.get(name.strip().lower(), "") or "-"
    values: dict[str, str] = {
        "ID": str(droplet.get("id", "")),
        "Name": name,
        "PublicIPv4": public_ipv4(droplet),
        "PrivateIPv4": private_ipv4(droplet),
        "Region": droplet_region_slug(droplet),
        "Status": str(droplet.get("status", "")),
        "Env": env_label,
    }
    return [values.get(col, "") for col in columns]


def _project_droplets(
    project_id: str,
    *,
    context: str | None,
) -> list[dict[str, Any]]:
    """Return droplet objects assigned to a project, sorted by id."""
    from catalpa_tooling.doctl_binary import run_doctl_json

    urns = list_project_resource_urns(project_id, context=context)
    droplet_ids = droplet_ids_from_resource_urns(urns)
    if not droplet_ids:
        return []

    all_droplets = run_doctl_json(["compute", "droplet", "list"], context=context)
    if not isinstance(all_droplets, list):
        all_droplets = []

    id_set = set(droplet_ids)
    matched = [
        d for d in all_droplets if isinstance(d, dict) and int(d.get("id", -1)) in id_set
    ]
    matched.sort(key=lambda d: int(d.get("id", 0)))
    return matched


def find_project_droplet_id_by_name(
    project_id: str,
    name: str,
    *,
    context: str | None,
) -> int | None:
    """Return the droplet id if ``name`` already exists in the project (case-insensitive)."""
    target = name.strip().lower()
    if not target:
        return None
    for droplet in _project_droplets(project_id, context=context):
        existing = str(droplet.get("name", "")).strip().lower()
        if existing == target:
            return int(droplet.get("id", 0))
    return None


def _print_droplet_table(
    droplets: list[dict[str, Any]],
    columns: tuple[str, ...],
    *,
    env_by_droplet_name: dict[str, str] | None = None,
) -> None:
    widths = [len(c) for c in columns]
    rows: list[list[str]] = []
    for d in droplets:
        row = _droplet_row(d, columns, env_by_droplet_name=env_by_droplet_name)
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
    config: ProjectConfig | None = None,
) -> int:
    matched = _project_droplets(project_id, context=context)
    if not matched:
        print("No droplets in this project.")
        return 0

    if as_json:
        print(json.dumps(matched, indent=2))
        return 0

    env_map: dict[str, str] | None = None
    if config is not None:
        from catalpa_tooling.deploy_do_link import droplet_name_to_env_map

        env_map = droplet_name_to_env_map(config)

    cols = columns or _DEFAULT_COLUMNS
    if env_map and "Env" not in cols:
        cols = cols + ("Env",)
    _print_droplet_table(matched, cols, env_by_droplet_name=env_map)
    return 0


def find_project_droplet_by_name(
    project_id: str,
    name: str,
    *,
    context: str | None,
) -> dict[str, Any] | None:
    """Return droplet dict in ``project_id`` whose name matches ``name`` (case-insensitive)."""
    target = name.strip().lower()
    if not target:
        return None
    for droplet in _project_droplets(project_id, context=context):
        existing = str(droplet.get("name", "")).strip().lower()
        if existing == target:
            return droplet
    return None
