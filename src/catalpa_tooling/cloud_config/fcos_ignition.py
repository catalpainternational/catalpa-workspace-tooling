"""Ignition for a Fedora CoreOS deploy droplet (`digitalocean.os: fedora-coreos`).

Fedora CoreOS reads Ignition, not cloud-init, so `host create` hands it this instead of
`droplet-bootstrap.yaml`. The host runs the stack as `core` on rootless Podman, which dk
reaches as ``ssh://core@<ip>/run/user/1000/podman/podman.sock``:

- `core` gets the account's SSH keys and **no passwordless sudo** (FCOS grants `%sudo
  NOPASSWD` by default; every container runs as `core`, so that made any escape root);
- linger, so `core`'s systemd and its Podman socket run with nobody logged in;
- `podman.socket`, and `podman-restart.service`, which starts containers with a restart
  policy at boot as Docker's daemon would;
- rootless binding of ports 80 and up, for the stack's Caddy;
- automatic updates reboot only in a weekly window, not mid-deploy.
"""

from __future__ import annotations

import json
from urllib.parse import quote

IGNITION_VERSION = "3.4.0"
CORE_UID = 1000
CORE_HOME = "/var/home/core"
USER_UNITS = f"{CORE_HOME}/.config/systemd/user"

NO_SUDO_FOR_CORE = (
    "# Written by dk host create. FCOS ships `%sudo ALL=(ALL) NOPASSWD: ALL` here; every\n"
    "# container runs as core, so that made escaping one root. Emptied on purpose.\n"
)
UNPRIVILEGED_PORTS = "net.ipv4.ip_unprivileged_port_start = 80\n"
UPDATE_WINDOW = (
    "[updates]\n"
    'strategy = "periodic"\n'
    "[[updates.periodic.window]]\n"
    'days = [ "Sun" ]\n'
    'start_time = "03:00"\n'
    "length_minutes = 60\n"
)


def _owner() -> dict[str, dict[str, int]]:
    return {"user": {"id": CORE_UID}, "group": {"id": CORE_UID}}


def _file(path: str, contents: str, mode: int, *, owned_by_core: bool = False) -> dict:
    entry: dict = {
        "path": path,
        "mode": mode,
        "overwrite": True,
        "contents": {"source": "data:," + quote(contents)},
    }
    if owned_by_core:
        entry.update(_owner())
    return entry


def render_fcos_ignition(ssh_public_keys: list[str]) -> str:
    """Ignition JSON for a rootless-Podman deploy host."""
    keys = [k.strip() for k in ssh_public_keys if k and k.strip()]
    if not keys:
        raise ValueError("a Fedora CoreOS droplet needs at least one SSH public key for core")
    directories = [
        {"path": p, "mode": 0o755, **_owner()}
        for p in (
            f"{CORE_HOME}/.config",
            f"{CORE_HOME}/.config/systemd",
            USER_UNITS,
            f"{USER_UNITS}/sockets.target.wants",
            f"{USER_UNITS}/default.target.wants",
        )
    ]
    links = [
        {
            "path": f"{USER_UNITS}/sockets.target.wants/podman.socket",
            "target": "/usr/lib/systemd/user/podman.socket",
            **_owner(),
        },
        {
            "path": f"{USER_UNITS}/default.target.wants/podman-restart.service",
            "target": "/usr/lib/systemd/user/podman-restart.service",
            **_owner(),
        },
    ]
    files = [
        _file("/etc/sudoers.d/coreos-sudo-group", NO_SUDO_FOR_CORE, 0o440),
        _file("/etc/sysctl.d/90-dk-unprivileged-ports.conf", UNPRIVILEGED_PORTS, 0o644),
        _file("/var/lib/systemd/linger/core", "", 0o644),
        _file("/etc/zincati/config.d/55-dk-update-window.toml", UPDATE_WINDOW, 0o644),
    ]
    config = {
        "ignition": {"version": IGNITION_VERSION},
        "passwd": {"users": [{"name": "core", "sshAuthorizedKeys": keys}]},
        "storage": {"directories": directories, "files": files, "links": links},
    }
    return json.dumps(config, indent=2) + "\n"
