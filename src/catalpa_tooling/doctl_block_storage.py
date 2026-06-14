"""DigitalOcean block storage helpers (``doctl compute volume``)."""

from __future__ import annotations

import sys
from typing import Any

from catalpa_tooling.doctl_binary import DoctlCommandError, run_doctl, run_doctl_json


def _volume_id(volume: dict[str, Any]) -> str:
    raw = volume.get("id")
    if raw is None:
        raise ValueError("volume record missing id")
    return str(raw)


def find_volume_by_name(name: str, *, context: str | None = None) -> dict[str, Any] | None:
    """Return DO volume dict when ``name`` matches, else ``None``."""
    data = run_doctl_json(["compute", "volume", "list"], context=context)
    if not isinstance(data, list):
        return None
    target = name.strip()
    for item in data:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip() == target:
            return item
    return None


def create_volume(
    name: str,
    size_gib: int,
    region: str,
    *,
    context: str | None = None,
) -> dict[str, Any]:
    """Create a block volume; return the volume record from doctl."""
    size_arg = f"{size_gib}GiB"
    result = run_doctl(
        [
            "compute",
            "volume",
            "create",
            name,
            "--size",
            size_arg,
            "--region",
            region,
            "--output",
            "json",
        ],
        context=context,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        from catalpa_tooling.doctl_binary import format_doctl_failure

        print(f"doctl volume create failed: {format_doctl_failure(result)}", file=sys.stderr)
        raise DoctlCommandError(
            format_doctl_failure(result),
            returncode=result.returncode,
        )
    import json

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise DoctlCommandError(f"invalid JSON from doctl: {exc}", returncode=1) from exc
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return first
    if isinstance(payload, dict):
        return payload
    raise DoctlCommandError("unexpected doctl volume create response", returncode=1)


def ensure_do_volume(
    name: str,
    size_gib: int,
    region: str,
    *,
    context: str | None = None,
) -> dict[str, Any]:
    """Return existing or newly created volume record."""
    existing = find_volume_by_name(name, context=context)
    if existing is not None:
        print(f"DO volume {name!r} already exists (id={_volume_id(existing)}).", file=sys.stderr)
        return existing
    print(
        f"Creating DO volume {name!r} ({size_gib} GiB in {region})…",
        file=sys.stderr,
    )
    return create_volume(name, size_gib, region, context=context)


def volume_attached_to_droplet(volume: dict[str, Any], droplet_id: str) -> bool:
    """True when ``volume`` droplet_ids includes ``droplet_id``."""
    ids = volume.get("droplet_ids")
    if not isinstance(ids, list):
        return False
    target = str(droplet_id).strip()
    return any(str(x).strip() == target for x in ids)


def attach_volume_to_droplet(
    volume_id: str,
    droplet_id: str,
    *,
    context: str | None = None,
    wait: bool = True,
) -> int:
    """Attach block volume to droplet; return process exit code."""
    args = [
        "compute",
        "volume-action",
        "attach",
        volume_id,
        droplet_id,
    ]
    if wait:
        args.append("--wait")
    result = run_doctl(args, context=context, check=False)
    if result.returncode != 0:
        from catalpa_tooling.doctl_binary import format_doctl_failure

        print(
            f"doctl volume attach failed: {format_doctl_failure(result)}",
            file=sys.stderr,
        )
    return result.returncode


def ensure_volume_attached(
    volume: dict[str, Any],
    droplet_id: str,
    *,
    context: str | None = None,
) -> int:
    """Attach ``volume`` to ``droplet_id`` when not already attached."""
    if volume_attached_to_droplet(volume, droplet_id):
        print(
            f"DO volume {_volume_id(volume)!r} already attached to droplet {droplet_id}.",
            file=sys.stderr,
        )
        return 0
    print(
        f"Attaching DO volume {_volume_id(volume)!r} to droplet {droplet_id}…",
        file=sys.stderr,
    )
    return attach_volume_to_droplet(_volume_id(volume), droplet_id, context=context)
