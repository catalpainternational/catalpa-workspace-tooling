"""``dk <env> host destroy``: tear an env's DigitalOcean host down (#69).

The inverse of ``host create`` (and of ``host provision`` for a rendered host): remove the DNS
records that point at the droplet, shut it down cleanly, detach and delete its volumes, delete
the droplet, and forget it locally (``docker_host`` when it named the droplet's IP, and the
known_hosts entries).

Only for an env marked disposable: ``digitalocean.allow_destroy: true`` in its ``info.yaml``.
No flag overrides that. It never takes a backup and never touches the backups (Spaces
buckets and keys), so a destroyed env can be rebuilt and restored.

Order matters:
1. DNS first, so no record points at an IP DigitalOcean hands to someone else.
2. A clean shutdown: a delete is a hard power cut, and file writes still in the page cache
   (media Django has just saved) can come back as empty files.
3. Volumes detached, then the droplet deleted. If the delete fails, stop: the volumes and the
   local state stay, and ``dk <env> host --sync-dns`` puts the DNS records back.
4. Volumes deleted, the droplet's cloud firewall ``dk-<droplet>`` deleted (unless another droplet
   is in it), local state cleared.

With the droplet already gone (an earlier ``--keep-volumes``), it deletes the env's volumes left
behind, detached, after the same confirmations. A volume attached to a droplet is never touched.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from catalpa_tooling.config import ProjectConfig


@dataclass
class DestroyPlan:
    droplet: dict[str, Any] | None
    ip: str = ""
    dns: list = field(default_factory=list)  # (HostDnsTarget, record)
    dns_notes: list[str] = field(default_factory=list)
    volumes: list[dict[str, Any]] = field(default_factory=list)
    missing_volumes: list[str] = field(default_factory=list)
    clear_docker_host: bool = False
    known_hosts: list[str] = field(default_factory=list)
    firewall: dict[str, Any] | None = None  # dk-<droplet>, when nothing else is in it
    firewall_note: str = ""


def _parser(env_name: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"dk {env_name} host destroy",
        description=(
            "Destroy this env's DigitalOcean host: its DNS A records, the droplet (after a clean "
            "shutdown) and its volumes, then forget it locally. Only for an env with "
            "digitalocean.allow_destroy: true. Takes no backup; never touches the backups."
        ),
    )
    p.add_argument("--dry-run", action="store_true", help="Show the plan; change nothing.")
    p.add_argument(
        "-y", "--yes", action="store_true",
        help="Don't ask. Fully unattended and irreversible: volume data is deleted too.",
    )  # fmt: skip
    p.add_argument("--keep-dns", action="store_true", help="Leave the DNS A records.")
    p.add_argument("--keep-volumes", action="store_true", help="Leave the volumes (and their data).")
    p.add_argument("--keep-firewall", action="store_true", help="Leave the cloud firewall dk-<droplet>.")
    p.add_argument(
        "--keep-docker-host", action="store_true", help="Leave docker_host and known_hosts as they are."
    )
    p.add_argument("--context", help="doctl authentication context")
    return p


def env_volume_names(config: ProjectConfig, env_name: str, info: dict[str, Any], droplet_name: str) -> list[str]:
    """The DigitalOcean volumes that belong to the env: a rendered host's data volume
    (``storage.data_volume.digitalocean``, default ``<hostname>-data``), or compose storage's DO
    volumes."""
    from catalpa_tooling.fcos.settings import FcosConfigError, parse_fcos_settings, runtime_of

    try:
        quadlet = runtime_of(info) == "quadlet"
    except FcosConfigError:
        quadlet = False
    if quadlet:
        from catalpa_tooling.fcos.render import _default_hostname

        s = parse_fcos_settings(info)
        if s.block_volume is False:
            return []
        if isinstance(s.block_volume, str):
            return [s.block_volume]
        return [f"{s.hostname or _default_hostname(config.meta.name, env_name)}-data"]
    from catalpa_tooling.host_storage import resolve_do_volume_names
    from catalpa_tooling.storage_config import parse_storage_volumes_from_info

    specs = parse_storage_volumes_from_info(info, config)
    return list(resolve_do_volume_names(info, specs, droplet_name=droplet_name).values())


def clear_info_docker_host(info_path: Path, *, dry_run: bool = False) -> int:
    """Remove the top-level ``docker_host:`` line from ``info.yaml``, keeping its comments
    (a YAML round trip would drop them)."""
    text = info_path.read_text(encoding="utf-8")
    new = re.sub(r"(?m)^docker_host:[^\n]*\n", "", text)
    if new == text:
        return 0
    if dry_run:
        print(f"dry-run: would remove docker_host from {info_path}", file=sys.stderr)
        return 0
    info_path.write_text(new, encoding="utf-8")
    print(f"Removed docker_host from {info_path}", file=sys.stderr)
    return 0


def _size(volume: dict[str, Any]) -> str:
    gib = volume.get("size_gigabytes")
    return f"{gib} GiB" if gib else "?"


def _print_plan(env_name: str, link, plan: DestroyPlan, ns) -> None:
    d = plan.droplet or {}
    from catalpa_tooling.deploy_do_link import droplet_region_slug

    print(f"\nDestroy {env_name}:", file=sys.stderr)
    print(
        f"  droplet   {link.droplet_name} (id {d.get('id')}, {droplet_region_slug(d)}, {plan.ip or 'no IPv4'})"
        "  shut down cleanly, then deleted",
        file=sys.stderr,
    )
    if ns.keep_dns:
        print("  DNS       kept (--keep-dns)", file=sys.stderr)
    for target, _rec in plan.dns:
        print(f"  DNS       A {target.hostname} -> {plan.ip}  removed", file=sys.stderr)
    for note in plan.dns_notes:
        print(f"  DNS       {note}", file=sys.stderr)
    for v in plan.volumes:
        action = "kept (--keep-volumes)" if ns.keep_volumes else "detached and DELETED, with its data"
        print(f"  volume    {v.get('name')} ({_size(v)})  {action}", file=sys.stderr)
    for name in plan.missing_volumes:
        print(f"  volume    {name}  not found (nothing to delete)", file=sys.stderr)
    _print_firewall(plan, ns)
    if ns.keep_docker_host:
        print("  local     docker_host and known_hosts kept (--keep-docker-host)", file=sys.stderr)
    else:
        if plan.clear_docker_host:
            print("  local     docker_host removed from info.yaml (it names the droplet's IP)", file=sys.stderr)
        for h in plan.known_hosts:
            print(f"  local     {h} removed from known_hosts", file=sys.stderr)
    print(
        "  No backup is taken. Backups (Spaces buckets and keys) aren't touched.\n",
        file=sys.stderr,
    )


def _forget_locally(info_path: Path, plan: DestroyPlan, *, dry_run: bool) -> int:
    from catalpa_tooling.ssh_known_hosts import remove_known_host

    rc = clear_info_docker_host(info_path, dry_run=dry_run) if plan.clear_docker_host else 0
    for h in plan.known_hosts:
        rc = rc or remove_known_host(h, dry_run=dry_run)
    return rc


def _local_plan(info: dict[str, Any], ip: str, plan: DestroyPlan) -> None:
    from catalpa_tooling.ssh_known_hosts import ssh_host_from_docker_host

    host = ssh_host_from_docker_host(str(info.get("docker_host") or "")) or ""
    # docker_host by IP was written by `host --write` for this droplet; by hostname, it stays
    # right for the next host behind the same name.
    plan.clear_docker_host = bool(host) and host == ip
    plan.known_hosts = [h for h in dict.fromkeys([ip, host]) if h]


def cmd_env_host_destroy(
    config: ProjectConfig,
    env_name: str,
    argv: list[str],
    *,
    global_dry_run: bool = False,
    global_yes: bool = False,
) -> int:
    from catalpa_tooling.deploy_do_link import (
        _orphan_droplet_message,
        _print_droplet_name_source,
        _truthy,
        find_droplet_for_link,
        is_digitalocean_host_disabled,
        load_env_info,
        public_ipv4,
        resolve_env_do_link,
    )
    from catalpa_tooling.doctl_binary import ensure_doctl_available

    p = _parser(env_name)
    ns, rest = p.parse_known_args(argv)
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")
    dry_run = global_dry_run or ns.dry_run
    yes = global_yes or ns.yes

    loaded = load_env_info(config, env_name)
    if loaded is None:
        return 1
    info_path, info = loaded
    raw_do = info.get("digitalocean") if isinstance(info.get("digitalocean"), dict) else {}
    if is_digitalocean_host_disabled(info):
        print(
            f"dk {env_name} host destroy is not available when digitalocean.disabled is set in "
            "info.yaml: dk doesn't manage this host on DigitalOcean.",
            file=sys.stderr,
        )
        return 1
    if not _truthy(raw_do.get("allow_destroy")):
        print(
            f"dk {env_name} host destroy is disabled for this environment. Set "
            f"`digitalocean.allow_destroy: true` in {info_path.relative_to(config.repo_root)} to "
            "mark this host as disposable.",
            file=sys.stderr,
        )
        return 1
    if not dry_run and not yes and not sys.stdin.isatty():
        print("Refusing host destroy without a TTY. Pass --yes if you intend it.", file=sys.stderr)
        return 1

    link = resolve_env_do_link(config, env_name, info)
    context = ns.context or (config.digitalocean.context if config.digitalocean else None)
    _print_droplet_name_source(config=config, env_name=env_name, link=link)
    ensure_doctl_available()

    droplet = find_droplet_for_link(config, link, context=context)
    if droplet is None:
        hint = _orphan_droplet_message(config, link, context=context, env_name=env_name)
        if hint:
            print(f"Refusing: {hint}", file=sys.stderr)
            return 1
        return _leftover_volumes(config, env_name, info, link, ns, context=context, dry_run=dry_run, yes=yes)

    plan = DestroyPlan(droplet=droplet, ip=public_ipv4(droplet))
    from catalpa_tooling.doctl_block_storage import find_volume_by_name
    from catalpa_tooling.doctl_domains import host_dns_records_at

    if not ns.keep_dns:
        plan.dns, plan.dns_notes = host_dns_records_at(info, droplet_ip=plan.ip, context=context)
    for name in env_volume_names(config, env_name, info, link.droplet_name):
        v = find_volume_by_name(name, context=context)
        (plan.volumes.append(v) if v else plan.missing_volumes.append(name))
    if not ns.keep_docker_host:
        _local_plan(info, plan.ip, plan)
    _firewall_plan(plan, link.droplet_name, str(droplet.get("id")), context=context)

    _print_plan(env_name, link, plan, ns)
    if dry_run:
        # The plan says it all; one line, not a "would run" per doctl command after it.
        print("dry-run: nothing changed; the plan above is what it would do.", file=sys.stderr)
        return 0
    if not yes:
        from catalpa_tooling.cli_confirm import confirm_by_typing_env_name, confirm_by_typing_expected

        if not confirm_by_typing_env_name(env_name):
            print("host destroy: cancelled.", file=sys.stderr)
            return 1
        if plan.volumes and not ns.keep_volumes and not confirm_by_typing_expected(
            "delete-volumes", hint="this deletes the volumes' data"
        ):
            print("host destroy: cancelled.", file=sys.stderr)
            return 1
    return _execute(config, env_name, info, info_path, plan, ns, context=context, dry_run=False)


def _firewall_plan(plan: DestroyPlan, droplet_name: str, droplet_id: str | None, *, context) -> None:
    """``dk-<droplet>`` goes with the droplet, unless another droplet is in it. A token that
    can't list firewalls leaves it, with a note: the destroy itself doesn't need firewalls."""
    from catalpa_tooling.doctl_binary import DoctlCommandError
    from catalpa_tooling.doctl_firewall import find_firewall, firewall_name

    name = firewall_name(droplet_name)
    try:
        fw = find_firewall(droplet_name, context=context)
    except DoctlCommandError:
        plan.firewall_note = f"{name}: can't list firewalls (the token lacks firewall:read); left alone"
        return
    if fw is None:
        return
    others = sorted({str(i) for i in fw.get("droplet_ids") or []} - {str(droplet_id)})
    if others:
        plan.firewall_note = f"{name}: droplet {', '.join(others)} is in it too; left alone"
        return
    plan.firewall = fw


def _print_firewall(plan: DestroyPlan, ns) -> None:
    if plan.firewall:
        action = "kept (--keep-firewall)" if ns.keep_firewall else "deleted"
        print(f"  firewall  {plan.firewall.get('name')}  {action}", file=sys.stderr)
    elif plan.firewall_note:
        print(f"  firewall  {plan.firewall_note}", file=sys.stderr)


def _leftover_volumes(config, env_name, info, link, ns, *, context, dry_run: bool, yes: bool) -> int:
    """The droplet is gone (an earlier destroy, perhaps with ``--keep-volumes``): delete what the
    env left behind, its detached volumes and its empty firewall. A volume attached to a droplet,
    or a firewall another droplet is in, is never touched: this env's droplet is gone, so they're
    in use by something else."""
    from catalpa_tooling.doctl_block_storage import delete_volume, find_volume_by_name

    print(f"No DigitalOcean droplet named {link.droplet_name!r}.", file=sys.stderr)
    found = [v for v in (find_volume_by_name(n, context=context) for n in env_volume_names(
        config, env_name, info, link.droplet_name)) if v]  # fmt: skip
    attached = [v for v in found if v.get("droplet_ids")]
    volumes = [v for v in found if not v.get("droplet_ids")]
    plan = DestroyPlan(droplet=None)
    if not ns.keep_firewall:
        _firewall_plan(plan, link.droplet_name, None, context=context)
    for v in attached:
        ids = ", ".join(str(x) for x in v["droplet_ids"])
        print(
            f"  volume    {v.get('name')} ({_size(v)})  attached to droplet {ids}, not this env's: left alone",
            file=sys.stderr,
        )
    if plan.firewall_note:
        print(f"  firewall  {plan.firewall_note}", file=sys.stderr)
    if ns.keep_volumes:
        volumes = []
    if not volumes and not plan.firewall:
        if attached:
            print("Refusing: nothing detached to delete.", file=sys.stderr)
            return 1
        print("Nothing to destroy.", file=sys.stderr)
        return 0
    print(f"\nDestroy what {env_name} left behind:", file=sys.stderr)
    for v in volumes:
        print(f"  volume    {v.get('name')} ({_size(v)})  DELETED, with its data", file=sys.stderr)
    if plan.firewall:
        print(f"  firewall  {plan.firewall.get('name')}  deleted", file=sys.stderr)
    print("  No backup is taken. Backups (Spaces buckets and keys) aren't touched.\n", file=sys.stderr)
    if dry_run:
        print("dry-run: nothing changed; the plan above is what it would do.", file=sys.stderr)
        return 0
    if not yes:
        from catalpa_tooling.cli_confirm import confirm_by_typing_env_name, confirm_by_typing_expected

        if not confirm_by_typing_env_name(env_name) or (
            volumes and not confirm_by_typing_expected("delete-volumes", hint="this deletes the volumes' data")
        ):
            print("host destroy: cancelled.", file=sys.stderr)
            return 1
    rc = 0
    for v in volumes:
        rc = delete_volume(str(v.get("id")), context=context) or rc
    if plan.firewall:
        from catalpa_tooling.doctl_firewall import delete_firewall

        rc = delete_firewall(str(plan.firewall.get("id")), context=context) or rc
    if rc == 0:
        print(f"{env_name}: what was left is deleted.", file=sys.stderr)
    return rc


def _execute(config, env_name, info, info_path, plan: DestroyPlan, ns, *, context, dry_run: bool) -> int:
    from catalpa_tooling.doctl_block_storage import delete_volume, detach_volume_from_droplet
    from catalpa_tooling.doctl_domains import remove_host_dns
    from catalpa_tooling.doctl_droplets import delete_droplet, shutdown_droplet

    droplet_id = str(plan.droplet.get("id"))
    failures: list[tuple[str, int]] = []

    if not ns.keep_dns and plan.dns:
        rc = remove_host_dns(info, droplet_ip=plan.ip, context=context, dry_run=dry_run)
        if rc:
            failures.append(("dns", rc))
    if shutdown_droplet(droplet_id, context=context, dry_run=dry_run) != 0:
        print("host destroy: the clean shutdown failed; deleting anyway (a hard power cut).", file=sys.stderr)
    volumes = [] if ns.keep_volumes else plan.volumes
    for v in volumes:
        if str(droplet_id) in [str(x) for x in (v.get("droplet_ids") or [])]:
            rc = detach_volume_from_droplet(str(v.get("id")), droplet_id, context=context, dry_run=dry_run)
            if rc:
                failures.append((f"detach {v.get('name')}", rc))
    rc = delete_droplet(droplet_id, context=context, dry_run=dry_run)
    if rc:
        print(
            f"host destroy: the droplet wasn't deleted, so its volumes and the local state are left "
            f"as they were. `dk {env_name} host --sync-dns` puts back any DNS records removed.",
            file=sys.stderr,
        )
        return rc
    for v in volumes:
        rc = delete_volume(str(v.get("id")), context=context, dry_run=dry_run)
        if rc:
            failures.append((f"delete volume {v.get('name')} (id {v.get('id')})", rc))
    if plan.firewall and not ns.keep_firewall:
        from catalpa_tooling.doctl_firewall import delete_firewall

        rc = delete_firewall(str(plan.firewall.get("id")), context=context, dry_run=dry_run)
        if rc:
            failures.append((f"delete firewall {plan.firewall.get('name')}", rc))
    if not ns.keep_docker_host:
        rc = _forget_locally(info_path, plan, dry_run=dry_run)
        if rc:
            failures.append(("known_hosts", rc))

    if failures:
        print("host destroy: the droplet is gone, but these failed:", file=sys.stderr)
        for what, code in failures:
            print(f"  {what} (exit {code})", file=sys.stderr)
        print("  Finish them with doctl (`doctl compute volume list`, `doctl compute domain records list`).", file=sys.stderr)
        return max(code for _w, code in failures)
    print("dry-run: nothing changed." if dry_run else f"{env_name}: host destroyed.", file=sys.stderr)
    return 0
