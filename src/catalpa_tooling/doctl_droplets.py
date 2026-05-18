"""Create DigitalOcean droplets with bundled cloud-config bootstrap."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from catalpa_tooling.cloud_config.render import DEFAULT_TIMEZONE, render_droplet_bootstrap
from catalpa_tooling.config import DigitalOceanConfig

DEFAULT_IMAGE = "ubuntu-24-04-x64"


def list_account_ssh_key_ids(*, context: str | None = None) -> tuple[str, ...]:
    """Return all SSH key IDs from ``doctl compute ssh-key list``."""
    from catalpa_tooling.doctl_binary import run_doctl_json

    data = run_doctl_json(["compute", "ssh-key", "list"], context=context)
    if not isinstance(data, list):
        return ()
    ids: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        key_id = item.get("id")
        if key_id is not None:
            ids.append(str(key_id))
    return tuple(ids)


def _pick(
    cli_value: str | None,
    manifest_value: str | None,
    *,
    field: str,
    required: bool = False,
    default: str | None = None,
) -> str:
    value = (cli_value or "").strip() or (manifest_value or "").strip() or (default or "").strip()
    if not value:
        if required:
            print(
                f"Missing {field}: pass a CLI flag or set digitalocean.{field} in tooling.yaml",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return ""
    return value


def _resolve_ssh_keys(
    cli_keys: tuple[str, ...],
    do_config: DigitalOceanConfig | None,
    *,
    context: str | None,
) -> tuple[str, ...]:
    if cli_keys:
        return cli_keys
    if do_config and do_config.ssh_keys:
        return do_config.ssh_keys
    keys = list_account_ssh_key_ids(context=context)
    if not keys:
        print(
            "No SSH keys on this DigitalOcean account. Add one, e.g.:\n"
            "  doctl compute ssh-key import my-key --public-key-file ~/.ssh/id_ed25519.pub",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(
        f"Using {len(keys)} SSH key(s) from account (doctl compute ssh-key list).",
        file=sys.stderr,
    )
    return keys


def _build_create_argv(
    name: str,
    *,
    size: str,
    image: str,
    region: str,
    project_id: str,
    ssh_keys: tuple[str, ...],
    user_data_path: Path,
    wait: bool,
) -> list[str]:
    args = [
        "compute",
        "droplet",
        "create",
        name,
        "--size",
        size,
        "--image",
        image,
        "--region",
        region,
        "--project-id",
        project_id,
        "--user-data-file",
        str(user_data_path),
    ]
    for key in ssh_keys:
        args.extend(["--ssh-keys", key])
    if wait:
        args.append("--wait")
    return args


def create_droplet(
    name: str,
    *,
    size: str | None = None,
    image: str | None = None,
    region: str | None = None,
    project_id: str,
    ssh_keys: tuple[str, ...] = (),
    timezone: str | None = None,
    context: str | None = None,
    wait: bool = False,
    dry_run: bool = False,
    do_config: DigitalOceanConfig | None = None,
) -> int:
    """Create a droplet with the standard bootstrap cloud-config user-data."""
    from catalpa_tooling.doctl_binary import ensure_doctl_available, run_doctl

    droplet_name = name.strip()
    if not droplet_name:
        print("Droplet name is required.", file=sys.stderr)
        return 1

    resolved_size = _pick(
        size,
        do_config.size if do_config else None,
        field="size",
        required=True,
    )
    resolved_image = _pick(
        image,
        do_config.image if do_config else None,
        field="image",
        default=DEFAULT_IMAGE,
    )
    resolved_region = _pick(
        region,
        do_config.region if do_config else None,
        field="region",
        required=True,
    )
    resolved_timezone = _pick(
        timezone,
        do_config.timezone if do_config else None,
        field="timezone",
        default=DEFAULT_TIMEZONE,
    )
    resolved_keys = _resolve_ssh_keys(ssh_keys, do_config, context=context)
    user_data = render_droplet_bootstrap(timezone=resolved_timezone)

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".yaml",
            delete=False,
            prefix="catalpa-cloud-config-",
        ) as tmp:
            tmp.write(user_data)
            tmp_path = Path(tmp.name)

        argv = _build_create_argv(
            droplet_name,
            size=resolved_size,
            image=resolved_image,
            region=resolved_region,
            project_id=project_id,
            ssh_keys=resolved_keys,
            user_data_path=tmp_path,
            wait=wait,
        )

        if dry_run:
            print(user_data, end="" if user_data.endswith("\n") else "\n")
            print("---", file=sys.stderr)
            print(f"$ doctl {' '.join(argv)}", file=sys.stderr)
            return 0

        ensure_doctl_available()
        result = run_doctl(argv, context=context)
        return result.returncode
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
