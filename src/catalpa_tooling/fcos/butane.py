"""``host.bu``: the Butane config that makes an FCOS host for one env.

Built from the render's install map, so every generated file is placed on the host exactly once
and nowhere else; Butane embeds them (``contents.local``) when it compiles the Ignition file:

    podman run --rm -v "$PWD":/work:Z -w /work quay.io/coreos/butane:release \\
        --strict --files-dir . host.bu > host.ign     # in generated/fcos/

What the host needs beyond the files, all from the rootless proof of concept (catalpa-site
docker/envs/fcos-rootless/host.bu, tested through rebuilds, power cuts and a restore):

- ``core`` for admins (passwordless sudo, ``fcos.admin_keys``) and the unprivileged account
  that runs the stack (``fcos.ssh_authorized_keys``),
  with fixed uid and pinned subordinate IDs, so the data directories' owners stay valid;
- the block storage volume, formatted on first boot only (a rebuilt host keeps its data);
- linger, the edge's socket and Podman's API socket, the timers;
- unprivileged ports from 80, the registry login kept in the account's home;
- Zincati's reboot window, hostname, time zone;
- hardening: SSH by key only, no root login, admins' keys apart from the account's (and
  not DigitalOcean's account keys); LLMNR and mDNS off.
"""

from __future__ import annotations

import posixpath

from catalpa_tooling.fcos.settings import (
    CORE_SUBUID_START,
    SUBUID_COUNT,
    SUBUID_START,
    FcosSettings,
)


#: Each login's keys live in one file dk manages; ``fcos apply`` removes any other file in the
#: directory (Ignition's, afterburn's, a key added by hand).
KEYS_FILE = "dk"
#: afterburn copies the droplet's DigitalOcean account keys to core at every boot.
MASKED_UNITS = ("afterburn-sshkeys@core.service",)


def home_of(user: str) -> str:
    return f"/var/home/{user}"


def keys_dir(user: str) -> str:
    return f"{home_of(user)}/.ssh/authorized_keys.d"


def key_files(settings: FcosSettings, header: str) -> tuple[dict[str, str], list[dict]]:
    """``core``'s and the account's ``authorized_keys.d/dk``, as install-map files, so a key
    change is a diff ``fcos apply`` can put on a running host."""
    s = settings
    if not s.admin_keys:
        raise ValueError(
            "fcos.admin_keys is empty: list the admins' public keys. They log in as core, which "
            "has passwordless sudo, so only people who may be root on the host"
        )
    if not s.ssh_authorized_keys:
        raise ValueError(
            f"fcos.ssh_authorized_keys is empty: list the public keys that log in as {s.user} "
            "(dk, CI): they run and deploy the stack, with no sudo"
        )
    files: dict[str, str] = {}
    install: list[dict] = []
    for user, keys, what in (
        ("core", s.admin_keys, "fcos.admin_keys: admins, root through sudo"),
        (s.user, s.ssh_authorized_keys, "fcos.ssh_authorized_keys: runs the stack, no sudo"),
    ):
        path = f"ssh/{user}.keys"
        files[path] = header + f"# {what}\n" + "".join(f"{k}\n" for k in keys)
        install.append(
            {"file": path, "dest": f"{keys_dir(user)}/{KEYS_FILE}", "owner": user, "mode": "0600"}
        )
    return files, install


def dir_mode(path: str) -> str:
    """sshd wants ``~/.ssh`` private; everything else in a home is 0755."""
    return "0700" if "/.ssh" in path else "0755"


def _owner(user: str | None) -> list[str]:
    if not user or user == "root":
        return []
    return ["      user:", f"        name: {user}", "      group:", f"        name: {user}"]


def _inline(text: str, indent: str = "          ") -> list[str]:
    return ["      contents:", "        inline: |"] + [
        (indent + line) if line else "" for line in text.rstrip("\n").split("\n")
    ]


def host_bu(
    *,
    settings: FcosSettings,
    header: str,
    hostname: str,
    timezone: str,
    install: list[dict],
    bind_volumes: dict[str, dict],
    proxy: str,
    timers: list[str],
    tmpfiles: list[str],
    monitor_dir: str | None,
    config_dir: str,
) -> str:
    s = settings
    out: list[str] = [
        header.rstrip("\n"),
        "# Compile in this directory:",
        '#   podman run --rm -v "$PWD":/work:Z -w /work quay.io/coreos/butane:release \\',
        "#     --strict --files-dir . host.bu > host.ign",
        "variant: fcos",
        "version: 1.6.0",
        "",
        "passwd:",
        "  users:",
        "    # Admins: passwordless sudo (FCOS default), so root-equivalent. Keys: fcos.admin_keys,",
        "    # installed below as ~/.ssh/authorized_keys.d/dk (so `fcos apply` can change them).",
        "    - name: core",
        "    # Runs the stack (rootless Podman) and is what dk connects as. Keys:",
        "    # fcos.ssh_authorized_keys. No sudo, no extra groups. A fixed uid: the quadlets live in",
        "    # /etc/containers/systemd/users/<uid>/.",
        f"    - name: {s.user}",
        f"      uid: {s.uid}",
        "",
        "storage:",
    ]
    if s.block_volume is not False:
        volume = s.block_volume or f"{hostname}-data"
        out += [
            "  # The DO block storage volume, attached when the droplet is created. Formatted on",
            "  # first boot only: an existing XFS filesystem with this label is kept.",
            "  filesystems:",
            f"    - device: /dev/disk/by-id/scsi-0DO_Volume_{volume}",
            "      format: xfs",
            "      label: data",
            "      wipe_filesystem: false",
            f"      path: {s.data_mount}",
            "      with_mount_unit: true",
            "",
        ]

    out += ["  directories:"]
    if s.block_volume is False:
        out += [f"    - path: {s.data_mount}", "      mode: 0755"]
    out.append("    # Bind volumes, owned by the host uid of the user that writes them (see manifest.json).")
    for _name, spec in sorted(bind_volumes.items()):
        if "owner_uid" not in spec:
            continue
        uid = spec["owner_uid"]
        out += [
            f"    - path: {spec['path']}",
            f"      mode: {spec['mode']}",
            "      user:",
            f"        id: {uid}",
            "      group:",
            f"        id: {uid}",
        ]
    if monitor_dir:
        from catalpa_tooling.fcos.monitoring import ZABBIX_GID

        out += [
            f"    # Backup status for the Zabbix agent: written by {s.user}, read by group {ZABBIX_GID}.",
            f"    - path: {monitor_dir}",
            "      mode: 0750",
            "      user:",
            f"        name: {s.user}",
            "      group:",
            f"        id: {ZABBIX_GID}",
        ]
    out += [
        f"    - path: {config_dir}",
        "      mode: 0755",
        f"    # Root-owned: {s.user} runs these quadlets but can't edit them.",
        f"    - path: {s.quadlet_dir}",
        "      mode: 0755",
        f"    # core's and {s.user}'s own directories. Ignition makes missing parents as root, so",
        "    # each level is listed.",
    ]
    owned: dict[str, str] = {
        f"{s.user_unit_dir}/sockets.target.wants": s.user,
        f"{s.user_unit_dir}/timers.target.wants": s.user,
    }
    for entry in install:
        if entry["owner"] not in ("root", ""):
            owned.setdefault(posixpath.dirname(entry["dest"]), entry["owner"])
    expanded: dict[str, str] = {}
    for d, user in owned.items():
        while d.startswith(home_of(user) + "/"):
            expanded[d] = user
            d = posixpath.dirname(d)
    for d, user in sorted(expanded.items()):
        out += [f"    - path: {d}", f"      mode: {dir_mode(d)}", *_owner(user)]
    out += [f"    - path: {s.home}/.config/containers", "      mode: 0700", *_owner(s.user)]

    out += [
        "",
        "  links:",
        "    - path: /etc/localtime",
        f"      target: ../usr/share/zoneinfo/{timezone}",
        "      overwrite: true",
        f"    # Enable {s.user}'s units (systemctl --user enable).",
        f"    - path: {s.user_unit_dir}/sockets.target.wants/{proxy}.socket",
        f"      target: ../{proxy}.socket",
        *_owner(s.user),
        "    # Docker-compatible API for dk.",
        f"    - path: {s.user_unit_dir}/sockets.target.wants/podman.socket",
        "      target: /usr/lib/systemd/user/podman.socket",
        *_owner(s.user),
    ]
    for timer in sorted(timers):
        out += [
            f"    - path: {s.user_unit_dir}/timers.target.wants/{timer}",
            f"      target: ../{timer}",
            *_owner(s.user),
        ]
    if s.auto_update:
        out += [
            "    # fcos.auto_update: podman auto-update for units marked AutoUpdate=registry.",
            f"    - path: {s.user_unit_dir}/timers.target.wants/podman-auto-update.timer",
            "      target: /usr/lib/systemd/user/podman-auto-update.timer",
            *_owner(s.user),
        ]

    w = s.update_window
    days = ", ".join(f'"{d}"' for d in w.days)
    out += ["", "  files:"]
    host_files: list[tuple[str, str, str]] = [
        (
            "# Pinned subordinate IDs (what useradd picks anyway), so a change in that order can't\n"
            "# move the data directories' owners.",
            "/etc/subuid",
            f"core:{CORE_SUBUID_START}:{SUBUID_COUNT}\n{s.user}:{SUBUID_START}:{SUBUID_COUNT}\n",
        ),
        ("", "/etc/subgid", f"core:{CORE_SUBUID_START}:{SUBUID_COUNT}\n{s.user}:{SUBUID_START}:{SUBUID_COUNT}\n"),
        ("# The DO image writes /etc/hostname before Ignition runs.", "/etc/hostname", f"{hostname}\n"),
        (
            f"# OS updates and their reboot only in this window ({timezone}).",
            "/etc/zincati/config.d/55-updates-strategy.toml",
            '[updates]\nstrategy = "periodic"\n\n[updates.periodic]\n'
            f'time_zone = "{timezone}"\n\n[[updates.periodic.window]]\n'
            f'days = [ {days} ]\nstart_time = "{w.start}"\nlength_minutes = {w.minutes}\n',
        ),
        (
            f"# {s.user}'s systemd (and so the stack) starts at boot with nobody logged in.",
            f"/var/lib/systemd/linger/{s.user}",
            "",
        ),
        (
            f"# Lets {s.user}'s systemd open 80 and 443 for the edge ({proxy}.socket).",
            "/etc/sysctl.d/50-unprivileged-ports.conf",
            "net.ipv4.ip_unprivileged_port_start = 80\n",
        ),
        (
            "# Keep rootless Podman's registry login across reboots (default: /run/user/<uid>).",
            "/etc/systemd/user.conf.d/10-registry-auth.conf",
            "[Manager]\nDefaultEnvironment=REGISTRY_AUTH_FILE="
            f"{s.home}/.config/containers/auth.json\n",
        ),
        (
            "# Hardening: keys only, no root, few tries. Read before FCOS's own drop-ins (first\n"
            "# value wins).",
            "/etc/ssh/sshd_config.d/10-hardening.conf",
            "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n"
            f"MaxAuthTries 3\nX11Forwarding no\nAllowUsers core {s.user}\n",
        ),
        (
            "# Hardening: no LLMNR or mDNS (resolved answers on 5355 by default).",
            "/etc/systemd/resolved.conf.d/10-no-llmnr.conf",
            "[Resolve]\nLLMNR=no\nMulticastDNS=no\n",
        ),
    ]
    if tmpfiles:
        host_files.append(
            (
                "# /run is emptied at boot: recreate the read-only Podman socket's directory.",
                f"/etc/tmpfiles.d/{posixpath.basename(config_dir)}.conf",
                "\n".join(tmpfiles) + "\n",
            )
        )
    for comment, path, text in host_files:
        if comment:
            out += [f"    {line}" for line in comment.split("\n")]
        out += [f"    - path: {path}", "      mode: 0644"]
        if path in ("/etc/subuid", "/etc/subgid", "/etc/hostname"):
            out.append("      overwrite: true")
        if text:
            out += _inline(text)
    out.append("    # Generated files (manifest.json's install map).")
    for entry in sorted(install, key=lambda e: e["dest"]):
        out += [f"    - path: {entry['dest']}", f"      mode: {entry['mode']}"]
        out += _owner(entry["owner"])
        out += ["      contents:", f"        local: {entry['file']}"]

    out += [
        "",
        "systemd:",
        "  units:",
        "    # Rootless quadlets wait for the system network-online.target, which nothing pulls in",
        "    # on a host without system quadlets: without this, the wait times out (90 s) at boot.",
        "    - name: multi-user.target",
        "      dropins:",
        "        - name: 10-network-online.conf",
        "          contents: |",
        "            [Unit]",
        "            Wants=network-online.target",
        "    # Only fcos.admin_keys log in as core: afterburn would add the droplet's DigitalOcean",
        "    # account keys to it at every boot.",
    ]
    for unit in MASKED_UNITS:
        out += [f"    - name: {unit}", "      mask: true"]
    return "\n".join(out) + "\n"
