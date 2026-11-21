"""``dk <env> host create`` for a rendered host (``runtime: quadlet``), on DigitalOcean.

The steps a person ran with ``doctl`` (tooling 2.0, M6), in one command:

1. The committed render is current (as ``host provision`` checks), and its Ignition fits
   DigitalOcean's 64 KiB of user data.
2. The droplet doesn't exist yet: Ignition runs only on a droplet's first boot.
3. The data volume (``storage.data_volume.digitalocean``, default ``<hostname>-data``): the
   env's detached one is reused, data and all (a rebuild after ``host destroy --keep-volumes``);
   one attached to a droplet is refused; otherwise it's created (``size_gib``) in the project.
4. The droplet, with the volume attached at creation (so Ignition finds and mounts it), the
   Ignition as user data, and the image, size and region from ``digitalocean.*``.
5. DNS: the env's A records point at the new IP. ``docker_host`` is updated only when it names an
   IP; a hostname stays right.
6. ``host provision`` against the new IP: it waits for SSH, checks the files, pushes the
   secrets, logs in to the registry and starts the stack (``--no-start`` for a restore).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from catalpa_tooling.config import ProjectConfig

#: DigitalOcean's limit on a droplet's user data.
USER_DATA_LIMIT = 64 * 1024


def _parser(env_name: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"dk {env_name} host create",
        description=(
            "Create this rendered host on DigitalOcean: its data volume (or reuse the one a "
            "`host destroy --keep-volumes` kept), the droplet booting the committed render's "
            "Ignition, the DNS records, then `host provision`."
        ),
    )
    p.add_argument("--size", help="Droplet size (default: digitalocean.size in info.yaml, then tooling.yaml)")
    p.add_argument(
        "--image",
        help="The account's Fedora CoreOS image ID (default: digitalocean.image in info.yaml, then tooling.yaml)",
    )
    p.add_argument("--region", help="Region (default: digitalocean.region in info.yaml, then tooling.yaml)")
    p.add_argument(
        "--ssh-key", action="append", dest="ssh_keys", default=[], metavar="ID|FINGERPRINT",
        help="DigitalOcean requires one; it gets no login (Ignition sets the keys). Default: the account's",
    )  # fmt: skip
    p.add_argument("--project", metavar="NAME|UUID", help="Project (default: digitalocean.* in tooling.yaml)")
    p.add_argument("--context", help="doctl authentication context")
    p.add_argument("--dry-run", action="store_true", help="Show the plan; create nothing.")
    p.add_argument(
        "--no-start",
        action="store_true",
        help="Provision but don't start the stack: restore the database and media first, then "
        "`host provision` starts it.",
    )
    p.add_argument("--no-provision", action="store_true", help="Stop once the droplet and DNS exist.")
    return p


def _docker_host_ip_replaced(info_path: Path, ip: str, *, dry_run: bool) -> None:
    """When ``docker_host`` names an IP (not a hostname), point it at ``ip``; keep comments."""
    text = info_path.read_text(encoding="utf-8")
    m = re.search(r"(?m)^(docker_host:\s*\S*?@)(\d{1,3}(?:\.\d{1,3}){3})([:/]\S*)?$", text)
    if not m or m.group(2) == ip:
        return
    new = text[: m.start(2)] + ip + text[m.end(2) :]
    if dry_run:
        print(f"dry-run: would point docker_host at {ip} in {info_path}", file=sys.stderr)
        return
    info_path.write_text(new, encoding="utf-8")
    print(f"docker_host now names {ip} ({info_path})", file=sys.stderr)


def _assign_volume_to_project(volume_id: str, project_id: str, *, context: str | None, dry_run: bool) -> int:
    from catalpa_tooling.doctl_binary import format_doctl_failure, run_doctl

    args = ["projects", "resources", "assign", project_id, "--resource", f"do:volume:{volume_id}"]
    if dry_run:
        print(f"dry-run: would run doctl {' '.join(args)}", file=sys.stderr)
        return 0
    r = run_doctl(args, context=context, capture_output=True, check=False)
    if r.returncode != 0:
        print(f"Assigning the volume to the project failed: {format_doctl_failure(r)}", file=sys.stderr)
    return r.returncode


def cmd_quadlet_host_create(
    config: ProjectConfig,
    env_name: str,
    info_path: Path,
    info: dict,
    argv: list[str],
    *,
    global_dry_run: bool = False,
) -> int:
    from catalpa_tooling.deploy_do_link import (
        _orphan_droplet_message,
        _print_droplet_name_source,
        find_droplet_for_link,
        public_ipv4,
        resolve_env_do_link,
    )
    from catalpa_tooling.doctl_projects import resolve_project_id, resolve_project_id_dry_run
    from catalpa_tooling.fcos import host as h
    from catalpa_tooling.fcos.cli import _check
    from catalpa_tooling.fcos.render import RenderError, generated_dir, render_env
    from catalpa_tooling.fcos.settings import parse_fcos_settings
    from catalpa_tooling.host_destroy import env_volume_names

    p = _parser(env_name)
    ns, rest = p.parse_known_args([a for a in argv if a != "--"])
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")
    dry_run = global_dry_run or ns.dry_run
    label = f"dk {env_name} host create"

    # 1. The render, and its Ignition.
    try:
        render = render_env(config, env_name)
    except RenderError as exc:
        print(f"{label}: {exc}", file=sys.stderr)
        return 1
    if _check(config, env_name, render, strict=False) != 0:
        print(f"{label}: not creating a host until the render is committed.", file=sys.stderr)
        return 1
    gen_dir = generated_dir(config, env_name)
    c = h.load_committed(gen_dir)
    try:
        ignition = h.compile_ignition(gen_dir).decode()
    except h.HostError as exc:
        print(f"{label}: {exc}", file=sys.stderr)
        return 1
    if len(ignition.encode()) > USER_DATA_LIMIT:
        print(
            f"{label}: the Ignition is {len(ignition.encode())} bytes; DigitalOcean takes "
            f"{USER_DATA_LIMIT} at most.",
            file=sys.stderr,
        )
        return 1

    from catalpa_tooling.deploy_do_link import _with_do_overrides

    # --project/--context: every step, before and after the droplet exists, in that account.
    config = _with_do_overrides(config, project=ns.project, context=ns.context)
    link = resolve_env_do_link(config, env_name, info)
    _print_droplet_name_source(config=config, env_name=env_name, link=link)
    do_config = config.digitalocean
    context = ns.context or (do_config.context if do_config else None)
    image = ns.image or link.image or (do_config.image if do_config else None)
    if not image:
        print(
            f"{label}: no Fedora CoreOS image. Set digitalocean.image in {info_path.name} to the "
            "account's FCOS custom image ID (`doctl compute image list-user`), or pass --image.",
            file=sys.stderr,
        )
        return 1
    region = ns.region or link.region or (do_config.region if do_config else None)
    if not region:
        print(f"{label}: no region: set digitalocean.region in {info_path.name}, or pass --region.", file=sys.stderr)
        return 1

    if dry_run:
        project_id = resolve_project_id_dry_run(ns.project, do_config=do_config)
    else:
        from catalpa_tooling.doctl_binary import ensure_doctl_available

        ensure_doctl_available()
        try:
            project_id = resolve_project_id(ns.project, do_config=do_config, context=context)
        except SystemExit:
            return 1

    # 2. No droplet yet.
    droplet = find_droplet_for_link(config, link, context=context)
    if droplet is not None:
        print(
            f"{label}: droplet {link.droplet_name!r} already exists (id {droplet.get('id')}, "
            f"{public_ipv4(droplet)}). Ignition runs only on first boot, so it can't be made "
            f"again in place: `dk {env_name} host provision` finishes one that exists, and "
            f"`dk {env_name} host destroy --keep-volumes` makes room for a new one.",
            file=sys.stderr,
        )
        return 1
    orphan = _orphan_droplet_message(config, link, context=context, env_name=env_name)
    if orphan:
        print(f"Refusing: {orphan}", file=sys.stderr)
        return 1

    # 3. The data volume.
    volume_ids: tuple[str, ...] = ()
    names = env_volume_names(config, env_name, info, link.droplet_name)
    if names:
        name = names[0]
        rc, vol_id = _data_volume(
            name, parse_fcos_settings(info).block_volume_size, region, project_id,
            context=context, dry_run=dry_run, label=label,
        )  # fmt: skip
        if rc != 0:
            return rc
        volume_ids = (vol_id,)

    # 4. The droplet.
    from catalpa_tooling.doctl_droplets import create_droplet

    try:
        rc = create_droplet(
            link.droplet_name,
            size=ns.size, image=image, region=region,
            env_size=link.size, env_region=link.region, env_image=None,
            project_id=project_id, ssh_keys=tuple(ns.ssh_keys), context=context,
            wait=True, dry_run=dry_run, do_config=do_config,
            enable_monitoring=False,  # no DO agent on Fedora CoreOS; Zabbix monitors it
            reuse_existing=False,
            user_data=ignition, user_data_label=f"Ignition from {gen_dir.relative_to(config.repo_root)}",
            volume_ids=volume_ids,
        )  # fmt: skip
    except SystemExit as exc:  # a missing size or region
        return int(exc.code or 1)
    if rc != 0:
        return rc
    if dry_run:
        print("dry-run: then point the DNS records at the new IP and run `host provision` on it.", file=sys.stderr)
        print("dry-run: nothing changed; the plan above is what it would do.", file=sys.stderr)
        return 0

    droplet = find_droplet_for_link(config, link, context=context)
    ip = public_ipv4(droplet or {})
    if not ip:
        print(f"{label}: the droplet has no public IPv4 yet: `dk {env_name} host` shows it.", file=sys.stderr)
        return 1

    # 5. DNS and docker_host.
    from catalpa_tooling.doctl_domains import sync_host_dns

    if sync_host_dns(config, info, droplet_ip=ip, context=context, dry_run=False) != 0:
        print(f"{label}: DNS failed; `dk {env_name} host --sync-dns` retries it.", file=sys.stderr)
    _docker_host_ip_replaced(info_path, ip, dry_run=False)

    if ns.no_provision:
        print(f"Next: dk {env_name} host provision --host {ip}", file=sys.stderr)
        return 0

    # 6. Provision.
    from catalpa_tooling.fcos.cli import _provision

    pns = argparse.Namespace(host=ip, no_start=ns.no_start)
    try:
        return _provision(pns, config, env_name, c, h.Host(ip), dry_run=False)
    except h.HostError as exc:
        print(f"{label}: {exc}", file=sys.stderr)
        print(f"The droplet exists; `dk {env_name} host provision --host {ip}` finishes it.", file=sys.stderr)
        return 1


def _data_volume(
    name: str, size_gib: int, region: str, project_id: str, *, context, dry_run: bool, label: str
) -> tuple[int, str]:
    """(rc, the volume's ID): the env's detached volume, or a new one."""
    from catalpa_tooling.doctl_binary import DoctlCommandError
    from catalpa_tooling.doctl_block_storage import create_volume, find_volume_by_name

    found = find_volume_by_name(name, context=context)
    if found is not None:
        vid = str(found.get("id"))
        if found.get("droplet_ids"):
            ids = ", ".join(str(x) for x in found["droplet_ids"])
            print(f"{label}: volume {name!r} is attached to droplet {ids}: refusing.", file=sys.stderr)
            return 1, ""
        vregion = (found.get("region") or {}).get("slug") if isinstance(found.get("region"), dict) else None
        if vregion and vregion != region:
            print(
                f"{label}: volume {name!r} is in {vregion}, the droplet would be in {region}: a "
                "volume attaches only in its own region.",
                file=sys.stderr,
            )
            return 1, ""
        print(
            f"Volume {name} ({found.get('size_gigabytes')} GiB, id {vid}) exists, detached: "
            "reusing it, with its data.",
            file=sys.stderr,
        )
        return 0, vid
    if dry_run:
        print(f"dry-run: would create volume {name} ({size_gib} GiB, {region}) in the project", file=sys.stderr)
        return 0, "<new-volume-id>"
    print(f"Creating volume {name} ({size_gib} GiB, {region}) …", file=sys.stderr)
    try:
        vol = create_volume(name, size_gib, region, context=context)
    except DoctlCommandError as exc:
        return exc.returncode or 1, ""
    vid = str(vol.get("id"))
    return _assign_volume_to_project(vid, project_id, context=context, dry_run=False), vid
