"""``dk <env> host create`` for a rendered host (``runtime: quadlet``), on DigitalOcean.

The steps a person ran with ``doctl`` (tooling 2.0, M6), in one command:

1. The committed render is current (as ``host provision`` checks), and its Ignition fits
   DigitalOcean's 64 KiB of user data.
2. The droplet doesn't exist yet: Ignition runs only on a droplet's first boot.
3. The data volume (``storage.data_volume.digitalocean``, default ``<hostname>-data``): the
   env's detached one is reused, data and all (a rebuild after ``host destroy --keep-volumes``);
   one attached to a droplet is refused; otherwise it's created (``size_gib``) in the project.
4. The droplet, with the volume attached at creation (so Ignition finds and mounts it), the
   Ignition as user data, and the image, size and region from ``digitalocean.*``; then its cloud
   firewall ``dk-<droplet>`` (``doctl_firewall``; ``digitalocean.firewall: false`` for none).
5. DNS: the env's A records point at the new IP. ``docker_host`` is updated only when it names an
   IP; a hostname stays right.
6. ``host provision`` against the new IP: it waits for SSH, checks the files, pushes the
   secrets, logs in to the registry and starts the stack.
7. On a **new** data volume, before the stack starts: restore the database and the media from
   whatever there is to restore, ``restore_from``'s backups or the env's own (a rebuilt prod
   continues its backup chain; starting empty would make a cluster its repository refuses).
   Nothing is lost, since the volume is empty, so nothing is asked. A reused volume keeps its
   data; ``--empty`` starts with none; a repository with no backup yet is skipped. Everything
   runs by IP, so a stale DNS answer on this machine can't get in the way.
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
    p.add_argument(
        "--empty",
        action="store_true",
        help="Start with no data even on a new volume: don't restore backups (a new env, a clean test).",
    )
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

    # 0. Every dk command after this one reaches the host through docker_host.
    from catalpa_tooling.ssh_known_hosts import ssh_host_from_docker_host

    if not ssh_host_from_docker_host(str(info.get("docker_host") or "")):
        origin = info.get("site_origin")
        first = origin[0] if isinstance(origin, list) and origin else origin
        host = str(first or "<host>").split("://")[-1].split("/")[0]
        print(
            f"{label}: info.yaml has no ssh:// docker_host, and dk's commands for this env reach "
            f"the host through it. Add, e.g.:\n  docker_host: ssh://deploy@{host}",
            file=sys.stderr,
        )
        return 1

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
    if not str(image).strip().isdigit():
        # Fedora CoreOS on DigitalOcean is a custom image, whose ID is a number. A slug is a
        # distribution image, often tooling.yaml's for compose envs (ubuntu-24-04-x64): the droplet
        # would boot it and ignore the Ignition.
        print(
            f"{label}: the image is {image!r}, a DigitalOcean distribution slug, not the "
            "account's Fedora CoreOS custom image (a number: `doctl compute image list-user`). "
            f"Set digitalocean.image in {info_path.name} (tooling.yaml's is for compose envs), "
            "or pass --image.",
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
    fresh_data = True  # the root disk of a new droplet is empty too
    if names:
        name = names[0]
        rc, vol_id, fresh_data = _data_volume(
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
    restore_note = _restore_plan(c, fresh_data=fresh_data, empty=ns.empty)
    if dry_run:
        fw_note = "" if env_firewall_settings_dry(info) else " (no cloud firewall: digitalocean.firewall: false)"
        print(
            f"dry-run: then put the cloud firewall dk-{link.droplet_name} on it{fw_note}, point the DNS "
            f"records at the new IP and run `host provision` on it. {restore_note}",
            file=sys.stderr,
        )
        print("dry-run: nothing changed; the plan above is what it would do.", file=sys.stderr)
        return 0

    droplet = find_droplet_for_link(config, link, context=context)
    ip = public_ipv4(droplet or {})
    if not ip:
        print(f"{label}: the droplet has no public IPv4 yet: `dk {env_name} host` shows it.", file=sys.stderr)
        return 1

    # The cloud firewall every new droplet gets (doctl_firewall): SSH, web, HTTP/3 and ping in;
    # deploy traffic out. A rendered host's Zabbix agent makes active checks only, so nothing
    # else comes in.
    from catalpa_tooling.doctl_firewall import ensure_droplet_firewall, env_firewall_settings

    fw = env_firewall_settings(info)
    if fw is None:
        print("No cloud firewall (digitalocean.firewall: false).", file=sys.stderr)
    elif ensure_droplet_firewall(link.droplet_name, droplet["id"], fw, context=context) != 0:
        print(f"{label}: the cloud firewall failed; the host is made, but unfiltered.", file=sys.stderr)
        return 1

    # 5. DNS and docker_host.
    from catalpa_tooling.doctl_domains import sync_host_dns

    if sync_host_dns(config, info, droplet_ip=ip, context=context, dry_run=False) != 0:
        print(f"{label}: DNS failed; `dk {env_name} host --sync-dns` retries it.", file=sys.stderr)
    _docker_host_ip_replaced(info_path, ip, dry_run=False)

    if ns.no_provision:
        print(f"Next: dk {env_name} host provision --host {ip}", file=sys.stderr)
        return 0

    # 6. Provision. The new droplet's keys go in known_hosts under its IP, then under docker_host's
    # name too, replacing an earlier host's: every later dk command connects by the name.
    from catalpa_tooling.fcos.cli import _provision
    from catalpa_tooling.ssh_known_hosts import alias_known_host, ensure_known_host

    target = h.Host(ip)
    if ensure_known_host(ip, port=target.port, recovery_env_name=env_name) != 0:
        print(f"The droplet exists; `dk {env_name} host provision --host {ip}` finishes it.", file=sys.stderr)
        return 1
    name = ssh_host_from_docker_host(str(info.get("docker_host") or ""))
    if alias_known_host(name, ip, port=target.port) != 0:
        print(
            f"{label}: couldn't copy {ip}'s host key to {name!r}; if ssh to {name} reports a changed "
            f"key, check it's {ip}'s and run `ssh-keygen -R {name}`.",
            file=sys.stderr,
        )

    restore = restore_note.startswith("Then restore")
    pns = argparse.Namespace(
        host=ip, no_start=ns.no_start or restore,
        no_start_note="Provisioned; the stack starts once the restore below is done." if restore else None,
    )  # fmt: skip
    try:
        rc = _provision(pns, config, env_name, c, target, dry_run=False)
        if rc != 0 or not restore:
            return rc
        return _restore_then_start(config, env_name, c, info, ip, no_start=ns.no_start, label=label)
    except h.HostError as exc:
        print(f"{label}: {exc}", file=sys.stderr)
        print(f"The droplet exists; `dk {env_name} host provision --host {ip}` finishes it.", file=sys.stderr)
        return 1


def _restore_plan(c, *, fresh_data: bool, empty: bool) -> str:
    """What happens to the data after provisioning, in a sentence ("Then restore …" when it
    restores)."""
    restore = c.manifest.get("restore") or {}
    own = any(k.upper().startswith(("PGBR_S3_WRITE_", "RESTIC_WRITE_")) for k in (c.manifest.get("secrets") or {}).values())
    if not (restore or own):
        return "Nothing to restore from (no backups: or restore_from:): the stack starts empty."
    if not fresh_data:
        return "The data volume is reused, so its data stays: no restore."
    if empty:
        return "--empty: no restore; the stack starts with no data."
    src = f"{restore['from']}'s backups" if restore.get("from") else "this env's own backups"
    return f"Then restore the database and media from {src} (what exists) before starting, as the data volume is new."


def _restore_then_start(config, env_name: str, c, info: dict, ip: str, *, no_start: bool, label: str) -> int:
    """After a --no-start provision on a new data volume: restore what the repositories hold,
    then start (unless --no-start) and run the post_db_restore hooks."""
    from catalpa_tooling.fcos import host as h
    from catalpa_tooling.fcos.backend import Backend, Usage

    b = Backend(config=config, env_name=env_name, committed=c, host=h.Host(ip), login=c.user, info=info)
    try:
        found = b.restorable()
    except Usage as exc:
        print(f"{label}: {exc}", file=sys.stderr)
        return 1
    kinds = tuple(k for k in ("db", "files") if found.get(k))
    for k in ("db", "files"):
        if k in found and not found[k]:
            what = {"db": "database backup", "files": "media snapshot"}[k]
            print(f"No {what} to restore yet: {k} starts empty.", file=sys.stderr)
    if kinds:
        src = c.manifest.get("restore", {}).get("from")
        owner = f"{src}'s" if src else "its own"
        what = " and ".join({"db": "the database", "files": "the media"}[k] for k in kinds)
        print(
            f"Restoring {what} from {owner} backups onto the new volume …",
            file=sys.stderr,
        )
        rc = b.restore_all([], "latest", yes=True, dry_run=False, kinds=kinds, then_start=True)
        if rc != 0:
            print(f"{label}: the restore failed; the host is made and not started. Fix it, then `dk {env_name} restore` and `dk {env_name} host provision`.", file=sys.stderr)
            return rc
    if no_start:
        print(f"Not started (--no-start): `dk {env_name} host provision` starts it.", file=sys.stderr)
        return 0
    # Provisioning is done (files, secrets, registry login): only the start is left.
    from catalpa_tooling.fcos.cli import start_stack

    rc = start_stack(c, h.Host(ip))
    if rc == 0 and "db" in kinds:
        rc = b._after_db_restore()
    return rc


def env_firewall_settings_dry(info: dict):
    from catalpa_tooling.doctl_firewall import env_firewall_settings

    return env_firewall_settings(info) is not None


def _data_volume(
    name: str, size_gib: int, region: str, project_id: str, *, context, dry_run: bool, label: str
) -> tuple[int, str, bool]:
    """(rc, the volume's ID, whether it's new): the env's detached volume, or a new one."""
    from catalpa_tooling.doctl_binary import DoctlCommandError
    from catalpa_tooling.doctl_block_storage import create_volume, find_volume_by_name

    found = find_volume_by_name(name, context=context)
    if found is not None:
        vid = str(found.get("id"))
        if found.get("droplet_ids"):
            ids = ", ".join(str(x) for x in found["droplet_ids"])
            print(f"{label}: volume {name!r} is attached to droplet {ids}: refusing.", file=sys.stderr)
            return 1, "", False
        vregion = (found.get("region") or {}).get("slug") if isinstance(found.get("region"), dict) else None
        if vregion and vregion != region:
            print(
                f"{label}: volume {name!r} is in {vregion}, the droplet would be in {region}: a "
                "volume attaches only in its own region.",
                file=sys.stderr,
            )
            return 1, "", False
        print(
            f"Volume {name} ({found.get('size_gigabytes')} GiB, id {vid}) exists, detached: "
            "reusing it, with its data.",
            file=sys.stderr,
        )
        return 0, vid, False
    if dry_run:
        print(f"dry-run: would create volume {name} ({size_gib} GiB, {region}) in the project", file=sys.stderr)
        return 0, "<new-volume-id>", True
    print(f"Creating volume {name} ({size_gib} GiB, {region}) …", file=sys.stderr)
    try:
        vol = create_volume(name, size_gib, region, context=context)
    except DoctlCommandError as exc:
        return exc.returncode or 1, "", True
    vid = str(vol.get("id"))
    return _assign_volume_to_project(vid, project_id, context=context, dry_run=False), vid, True
