"""Create DigitalOcean droplets with bundled cloud-config bootstrap."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from catalpa_tooling.cloud_config.render import DEFAULT_TIMEZONE, render_droplet_bootstrap
from catalpa_tooling.config import DigitalOceanConfig
from catalpa_tooling.doctl_binary import DoctlCommandError, DoctlNotFoundError

DEFAULT_IMAGE = "ubuntu-24-04-x64"


def _hint_ssh_key_list_forbidden() -> None:
    print(
        "Cannot list account SSH keys (403). Add ssh_key:read to your DigitalOcean "
        "API token (GET /v2/account/keys — not account:read), or pass keys explicitly:\n"
        "  dk <env> host create --ssh-key ID   # repeatable\n"
        "  digitalocean.ssh_keys in tooling.yaml",
        file=sys.stderr,
    )


def list_account_ssh_key_ids(*, context: str | None = None) -> tuple[str, ...]:
    """Return all SSH key IDs from ``doctl compute ssh-key list``."""
    from catalpa_tooling.doctl_binary import run_doctl_json

    try:
        data = run_doctl_json(["compute", "ssh-key", "list"], context=context)
    except DoctlCommandError as e:
        if e.returncode == 403 or "403" in str(e) or "not authorized" in str(e).lower():
            _hint_ssh_key_list_forbidden()
        raise
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
    env_value: str | None = None,
    field: str,
    required: bool = False,
    default: str | None = None,
) -> str:
    value = (
        (cli_value or "").strip()
        or (env_value or "").strip()
        or (manifest_value or "").strip()
        or (default or "").strip()
    )
    if not value:
        if required:
            print(
                f"Missing {field}: pass --{field}, set digitalocean.{field} in "
                f"docker/envs/<env>/info.yaml, or digitalocean.{field} in tooling.yaml",
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
    dry_run: bool = False,
) -> tuple[str, ...]:
    if cli_keys:
        return cli_keys
    if do_config and do_config.ssh_keys:
        return do_config.ssh_keys
    if dry_run:
        try:
            keys = list_account_ssh_key_ids(context=context)
        except DoctlNotFoundError:
            print(
                "dry-run: no --ssh-key or digitalocean.ssh_keys; "
                "host doctl would embed all account SSH keys (install doctl to list them).",
                file=sys.stderr,
            )
            return ()
        if keys:
            print(
                f"Using {len(keys)} SSH key(s) from account (doctl compute ssh-key list).",
                file=sys.stderr,
            )
            return keys
        print(
            "dry-run: no SSH keys on account; pass --ssh-key or set digitalocean.ssh_keys.",
            file=sys.stderr,
        )
        return ()
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
    enable_monitoring: bool,
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
    if enable_monitoring:
        args.append("--enable-monitoring")
    if wait:
        args.append("--wait")
    return args


def _created_droplets_from_json(data: object) -> list[dict[str, object]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _print_created_droplet_summary(droplet: dict[str, object]) -> None:
    from catalpa_tooling.deploy_do_link import droplet_region_slug, public_ipv4

    droplet_id = droplet.get("id", "")
    name = droplet.get("name", "")
    status = droplet.get("status", "")
    region = droplet_region_slug(droplet)  # type: ignore[arg-type]
    ip = public_ipv4(droplet)  # type: ignore[arg-type]
    print(
        f"Created droplet {name!r} (id {droplet_id}, {region}, {status}, public {ip})",
        file=sys.stderr,
    )


def _ensure_created_droplets_in_project(
    droplets: list[dict[str, object]],
    *,
    project_id: str,
    droplet_name: str,
    context: str | None,
    dry_run: bool,
) -> int:
    from catalpa_tooling.doctl_binary import DoctlCommandError
    from catalpa_tooling.doctl_projects import (
        ensure_droplet_in_project,
        wait_for_project_droplet_by_name,
    )

    for droplet in droplets:
        droplet_id = int(droplet.get("id", 0) or 0)
        if droplet_id <= 0:
            print("Droplet create returned no id.", file=sys.stderr)
            return 1
        try:
            ensure_droplet_in_project(
                droplet_id,
                project_id,
                context=context,
                dry_run=dry_run,
            )
        except DoctlCommandError as e:
            print(str(e), file=sys.stderr)
            return e.returncode

    if dry_run:
        return 0

    verified = wait_for_project_droplet_by_name(
        project_id,
        droplet_name,
        context=context,
    )
    if verified is None:
        ids = ", ".join(str(d.get("id", "")) for d in droplets)
        print(
            f"Droplet {droplet_name!r} was created (id {ids}) but is not visible in "
            f"project {project_id!r} after assign.\n"
            "Ensure the API token includes project:update (or Full Access), then assign "
            "manually, e.g.:\n"
            f"  doctl projects resources assign {project_id} "
            f"--resource=do:droplet:<id>",
            file=sys.stderr,
        )
        return 1
    return 0


def _resolve_existing_droplet_for_create(
    droplet_name: str,
    *,
    project_id: str,
    context: str | None,
    reuse_existing: bool,
) -> int | None:
    """Return exit code when an existing droplet short-circuits create, else None."""
    from catalpa_tooling.deploy_do_link import find_droplet_by_name
    from catalpa_tooling.doctl_projects import (
        ensure_droplet_in_project,
        find_project_droplet_id_by_name,
        wait_for_project_droplet_by_name,
    )

    existing_id = find_project_droplet_id_by_name(
        project_id,
        droplet_name,
        context=context,
    )
    if existing_id is not None:
        if reuse_existing:
            print(
                f"Droplet {droplet_name!r} already exists in this project "
                f"(id {existing_id}); continuing provisioning.",
                file=sys.stderr,
            )
            return 0
        print(
            f"Droplet {droplet_name!r} already exists in this project (id {existing_id}). "
            "Choose another name or remove the existing droplet.",
            file=sys.stderr,
        )
        return 1

    global_droplet = find_droplet_by_name(droplet_name, context=context)
    if global_droplet is None:
        return None

    global_id = int(global_droplet.get("id", 0) or 0)
    if global_id <= 0:
        return None

    if reuse_existing:
        from catalpa_tooling.doctl_binary import DoctlCommandError

        print(
            f"Droplet {droplet_name!r} exists outside this project (id {global_id}); "
            "assigning to project and continuing provisioning.",
            file=sys.stderr,
        )
        try:
            ensure_droplet_in_project(
                global_id,
                project_id,
                context=context,
                dry_run=False,
            )
        except DoctlCommandError as e:
            print(str(e), file=sys.stderr)
            return e.returncode
        if wait_for_project_droplet_by_name(
            project_id,
            droplet_name,
            context=context,
        ) is None:
            print(
                f"Droplet {droplet_name!r} (id {global_id}) could not be verified in "
                f"project {project_id!r} after assign.",
                file=sys.stderr,
            )
            return 1
        return 0

    print(
        f"Droplet {droplet_name!r} already exists on this account (id {global_id}) but "
        "is not in the configured project.\n"
        f"Assign it, e.g.:\n"
        f"  doctl projects resources assign {project_id} "
        f"--resource=do:droplet:{global_id}\n"
        "Or re-run with reuse enabled (default): dk <env> host create",
        file=sys.stderr,
    )
    return 1


def create_droplet(
    name: str,
    *,
    size: str | None = None,
    image: str | None = None,
    region: str | None = None,
    env_size: str | None = None,
    env_region: str | None = None,
    project_id: str,
    ssh_keys: tuple[str, ...] = (),
    timezone: str | None = None,
    context: str | None = None,
    wait: bool = False,
    dry_run: bool = False,
    do_config: DigitalOceanConfig | None = None,
    for_env: str | None = None,
    enable_monitoring: bool | None = None,
    reuse_existing: bool = False,
) -> int:
    """Create a droplet with the standard bootstrap cloud-config user-data."""
    from catalpa_tooling.deploy_do_link import normalize_droplet_hostname
    from catalpa_tooling.doctl_binary import (
        DoctlCommandError,
        ensure_doctl_available,
        run_doctl_json,
    )

    droplet_name = normalize_droplet_hostname(name.strip())
    if not droplet_name:
        print("Droplet name is required.", file=sys.stderr)
        return 1

    resolved_size = _pick(
        size,
        do_config.size if do_config else None,
        env_value=env_size,
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
        env_value=env_region,
        field="region",
        required=True,
    )
    resolved_timezone = _pick(
        timezone,
        do_config.timezone if do_config else None,
        field="timezone",
        default=DEFAULT_TIMEZONE,
    )
    resolved_keys = _resolve_ssh_keys(
        ssh_keys, do_config, context=context, dry_run=dry_run
    )
    resolved_monitoring = (
        enable_monitoring
        if enable_monitoring is not None
        else (do_config.monitoring if do_config else True)
    )
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
            enable_monitoring=resolved_monitoring,
        )

        if dry_run:
            print(user_data, end="" if user_data.endswith("\n") else "\n")
            print("---", file=sys.stderr)
            print(f"host doctl command: doctl {' '.join(argv)}", file=sys.stderr)
            from catalpa_tooling.doctl_projects import ensure_droplet_in_project

            ensure_droplet_in_project(
                0,
                project_id,
                context=context,
                dry_run=True,
            )
            return 0

        existing_rc = _resolve_existing_droplet_for_create(
            droplet_name,
            project_id=project_id,
            context=context,
            reuse_existing=reuse_existing,
        )
        if existing_rc is not None:
            return existing_rc

        ensure_doctl_available()
        try:
            data = run_doctl_json(argv, context=context)
        except DoctlCommandError as e:
            print(str(e), file=sys.stderr)
            return e.returncode

        created = _created_droplets_from_json(data)
        if not created:
            print("Droplet create returned no droplets.", file=sys.stderr)
            return 1

        for droplet in created:
            _print_created_droplet_summary(droplet)

        rc = _ensure_created_droplets_in_project(
            created,
            project_id=project_id,
            droplet_name=droplet_name,
            context=context,
            dry_run=False,
        )
        if rc != 0:
            return rc

        if for_env:
            from catalpa_tooling.deploy_do_link import suggest_host_write_command

            print(
                f"Next: {suggest_host_write_command(for_env)}",
                file=sys.stderr,
            )
        return 0
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
