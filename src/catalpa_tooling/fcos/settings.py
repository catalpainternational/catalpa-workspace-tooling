"""``runtime`` and the ``fcos:`` block of ``docker/envs/<env>/info.yaml``.

.. code-block:: yaml

    runtime: quadlet            # compose (default) | quadlet
    fcos:
      user: deploy              # the unprivileged account the stack runs as (default deploy)
      uid: 1001                 # its uid (default 1001: the first account after core)
      data_mount: /var/mnt/data # where the block storage volume is mounted
      bind_volumes: [pgdata, django_media, caddy_data]
                                # compose volumes kept on data_mount (default: the ops
                                # pgbackrest/restic data volumes and caddy_data)
      id_ranges: {db: 10000}    # pin a group's container ID range (keyed by any of its services)
      auto_update: false        # podman auto-update from the registry (staging/test sites only)
      drop_ports: []            # services whose compose `ports` are dropped rather than refused
      caddyfile: /etc/caddy/Caddyfile  # the Caddyfile's path inside the edge image

Everything else (images, settings, credentials, backups) comes from the keys ``dk`` already
reads for compose deploys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

RUNTIMES = ("compose", "quadlet")

#: deploy's first subordinate ID, pinned in /etc/subuid and /etc/subgid by the host config.
#: FCOS gives core 524288-589823, and the next account 589824 onwards.
SUBUID_START = 589824
SUBUID_COUNT = 65536
#: Each group of containers gets its own ID range in deploy's user namespace.
ID_RANGE_SIZE = 5000
ID_RANGE_FIRST = 10000


class FcosConfigError(ValueError):
    """Invalid ``runtime`` or ``fcos:`` in info.yaml."""


@dataclass(frozen=True)
class FcosSettings:
    user: str = "deploy"
    uid: int = 1001
    data_mount: str = "/var/mnt/data"
    bind_volumes: tuple[str, ...] | None = None
    id_ranges: dict[str, int] = field(default_factory=dict)
    auto_update: bool = False
    drop_ports: tuple[str, ...] = ()
    caddyfile: str = "/etc/caddy/Caddyfile"

    @property
    def home(self) -> str:
        return f"/var/home/{self.user}"

    @property
    def quadlet_dir(self) -> str:
        """Root-owned, so the account cannot change what it runs."""
        return f"/etc/containers/systemd/users/{self.uid}"

    @property
    def user_unit_dir(self) -> str:
        return f"{self.home}/.config/systemd/user"


_KEYS = {
    "user",
    "uid",
    "data_mount",
    "bind_volumes",
    "id_ranges",
    "auto_update",
    "drop_ports",
    "caddyfile",
}
_NAME = re.compile(r"^[a-z_][a-z0-9_-]*$")


def runtime_of(info: dict) -> str:
    raw = info.get("runtime", "compose")
    runtime = str(raw).strip() if raw is not None else "compose"
    if runtime not in RUNTIMES:
        raise FcosConfigError(f"runtime must be one of {', '.join(RUNTIMES)} (got {raw!r})")
    return runtime


def _str_list(raw: Any, field_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(v, str) and v.strip() for v in raw):
        raise FcosConfigError(f"fcos.{field_name} must be a list of names")
    return tuple(v.strip() for v in raw)


def parse_fcos_settings(info: dict) -> FcosSettings:
    raw = info.get("fcos")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise FcosConfigError("fcos must be a mapping")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise FcosConfigError(f"unknown fcos keys: {', '.join(unknown)}")

    user = str(raw.get("user", "deploy")).strip()
    if not _NAME.fullmatch(user) or user in ("root", "core"):
        raise FcosConfigError(f"fcos.user must be an unprivileged account name (got {user!r})")
    uid = raw.get("uid", 1001)
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 1000:
        raise FcosConfigError(f"fcos.uid must be an integer >= 1000 (got {uid!r})")
    data_mount = str(raw.get("data_mount", "/var/mnt/data")).rstrip("/")
    if not data_mount.startswith("/") or ".." in data_mount.split("/"):
        raise FcosConfigError(f"fcos.data_mount must be an absolute path (got {data_mount!r})")

    bind_raw = raw.get("bind_volumes")
    bind_volumes = None if bind_raw is None else _str_list(bind_raw, "bind_volumes")

    ranges_raw = raw.get("id_ranges") or {}
    if not isinstance(ranges_raw, dict):
        raise FcosConfigError("fcos.id_ranges must map a service name to its range's first ID")
    id_ranges: dict[str, int] = {}
    for svc, start in ranges_raw.items():
        if not isinstance(start, int) or isinstance(start, bool) or start < 1:
            raise FcosConfigError(f"fcos.id_ranges.{svc} must be a positive integer")
        if start + ID_RANGE_SIZE - 1 > SUBUID_COUNT:
            raise FcosConfigError(
                f"fcos.id_ranges.{svc}: {start} + {ID_RANGE_SIZE} IDs goes past the account's "
                f"{SUBUID_COUNT} subordinate IDs"
            )
        id_ranges[str(svc)] = start

    auto_update = raw.get("auto_update", False)
    if not isinstance(auto_update, bool):
        raise FcosConfigError("fcos.auto_update must be true or false")
    caddyfile = str(raw.get("caddyfile", "/etc/caddy/Caddyfile"))
    if not caddyfile.startswith("/"):
        raise FcosConfigError("fcos.caddyfile must be an absolute path inside the edge image")

    return FcosSettings(
        user=user,
        uid=uid,
        data_mount=data_mount,
        bind_volumes=bind_volumes,
        id_ranges=id_ranges,
        auto_update=auto_update,
        drop_ports=_str_list(raw.get("drop_ports"), "drop_ports"),
        caddyfile=caddyfile,
    )
