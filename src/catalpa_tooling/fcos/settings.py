"""``runtime`` and the ``fcos:`` block of ``docker/envs/<env>/info.yaml``.

.. code-block:: yaml

    runtime: quadlet            # compose (default) | quadlet
    storage:
      data_volume:              # shared with compose's storage settings (docs/TOOLING_2.md)
        path: /var/mnt/data     # the data directory (default /var/mnt/data)
        digitalocean:           # the DO volume under it; absent: a directory on the root disk
          name: catalpa-site-prod-data  # default <hostname>-data
          size_gib: 10          # when `host create` makes it
          filesystem: xfs       # default xfs
      # No storage.data_volume at all: /var/mnt/data on a 10 GiB <hostname>-data volume.
    fcos:
      user: deploy              # the unprivileged account the stack runs as (default deploy)
      uid: 1001                 # its uid (default 1001: the first account after core)
      bind_volumes: [pgdata, django_media, caddy_data]
                                # compose volumes kept in the data directory (default: the ops
                                # pgbackrest/restic data volumes and caddy_data)
      volume_owners: {pgdata: 999}
                                # the uid inside the container that owns a bind volume's
                                # directory (default 0; the pgBackRest data volume 999)
      id_ranges: {db: 10000}    # pin a group's container ID range (keyed by any of its services)
      auto_update: false        # podman auto-update from the registry (staging/test sites only)
      drop_ports: []            # services whose compose `ports` are dropped rather than refused
      caddyfile: /etc/caddy/Caddyfile  # the Caddyfile's path inside the edge image
      # The host (host.bu):
      hostname: catalpa-site-prod   # default <project>-<env>
      timezone: Asia/Dili       # default: tooling.yaml digitalocean.timezone, else UTC
      admin_keys: [...]         # public keys for core: admins, root through sudo
      ssh_authorized_keys: [...]  # public keys for the account (dk, CI): no sudo
      update_window: {days: [Sun], start: "02:00", minutes: 60}  # Zincati reboots
      zabbix_image: docker.io/zabbix/zabbix-agent2:alpine-7.0-latest

Everything else (images, settings, credentials, backups, monitoring) comes from the keys ``dk``
already reads for compose deploys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

RUNTIMES = ("compose", "quadlet")

#: deploy's first subordinate ID, pinned in /etc/subuid and /etc/subgid by the host config.
#: FCOS gives core 524288-589823, and the next account 589824 onwards.
CORE_SUBUID_START = 524288
SUBUID_START = 589824
SUBUID_COUNT = 65536
#: Each group of containers gets its own ID range in deploy's user namespace.
ID_RANGE_SIZE = 5000
ID_RANGE_FIRST = 10000
#: Pinned to the Zabbix server's series: a newer agent than the server isn't supported.
DEFAULT_ZABBIX_IMAGE = "docker.io/zabbix/zabbix-agent2:alpine-7.0-latest"


class FcosConfigError(ValueError):
    """Invalid ``runtime`` or ``fcos:`` in info.yaml."""


@dataclass(frozen=True)
class UpdateWindow:
    days: tuple[str, ...] = ("Sun",)
    start: str = "02:00"
    minutes: int = 60


@dataclass(frozen=True)
class FcosSettings:
    user: str = "deploy"
    uid: int = 1001
    #: From ``storage.data_volume`` (shared with compose's storage settings): where the data
    #: directory is, and the DO volume under it (None: ``<hostname>-data``; False: none).
    data_mount: str = "/var/mnt/data"
    bind_volumes: tuple[str, ...] | None = None
    volume_owners: dict[str, int] = field(default_factory=dict)
    id_ranges: dict[str, int] = field(default_factory=dict)
    auto_update: bool = False
    drop_ports: tuple[str, ...] = ()
    caddyfile: str = "/etc/caddy/Caddyfile"
    hostname: str | None = None
    timezone: str | None = None
    block_volume: str | bool | None = None
    block_volume_size: int = 10
    block_volume_filesystem: str = "xfs"
    ssh_authorized_keys: tuple[str, ...] = ()
    admin_keys: tuple[str, ...] = ()
    update_window: UpdateWindow = UpdateWindow()
    zabbix_image: str = DEFAULT_ZABBIX_IMAGE

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
    "bind_volumes",
    "volume_owners",
    "id_ranges",
    "auto_update",
    "drop_ports",
    "caddyfile",
    "hostname",
    "timezone",
    "ssh_authorized_keys",
    "admin_keys",
    "update_window",
    "zabbix_image",
}
_NAME = re.compile(r"^[a-z_][a-z0-9_-]*$")
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


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
        raise FcosConfigError(f"fcos.{field_name} must be a list of strings")
    return tuple(v.strip() for v in raw)


def _int_map(raw: Any, field_name: str, *, low: int, high: int) -> dict[str, int]:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise FcosConfigError(f"fcos.{field_name} must be a mapping")
    out: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise FcosConfigError(f"fcos.{field_name}.{key} must be an integer {low}-{high}")
        out[str(key)] = value
    return out


def _update_window(raw: Any) -> UpdateWindow:
    if raw is None:
        return UpdateWindow()
    if not isinstance(raw, dict) or set(raw) - {"days", "start", "minutes"}:
        raise FcosConfigError("fcos.update_window takes days, start and minutes")
    days = _str_list(raw.get("days", ["Sun"]), "update_window.days")
    if not days or set(days) - set(_DAYS):
        raise FcosConfigError(f"fcos.update_window.days: use {', '.join(_DAYS)}")
    start = str(raw.get("start", "02:00"))
    if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", start):
        raise FcosConfigError("fcos.update_window.start must be HH:MM")
    minutes = raw.get("minutes", 60)
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 30 <= minutes <= 1440:
        raise FcosConfigError("fcos.update_window.minutes must be 30-1440")
    return UpdateWindow(days=days, start=start, minutes=minutes)


def _public_keys(raw: dict, field_name: str) -> tuple[str, ...]:
    keys = _str_list(raw.get(field_name), field_name)
    for key in keys:
        if not re.match(r"^(ssh-(ed25519|rsa)|ecdsa-sha2-nistp\d+|sk-\S+) [A-Za-z0-9+/=]+( .*)?$", key):
            raise FcosConfigError(f"fcos.{field_name}: not a public key: {key[:40]!r}…")
    return keys


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
    from catalpa_tooling.storage_config import (
        DEFAULT_DATA_VOLUME_FILESYSTEM,
        DEFAULT_DATA_VOLUME_PATH,
        DEFAULT_DATA_VOLUME_SIZE_GIB,
        StorageConfigError,
        parse_data_volume,
    )

    storage = info.get("storage")
    if isinstance(storage, dict) and storage.get("volumes"):
        raise FcosConfigError(
            "storage.volumes is compose's (a volume per mount); a quadlet env keeps all its data "
            "in one directory: storage.data_volume"
        )
    try:
        data_volume = parse_data_volume(info)
    except StorageConfigError as exc:
        raise FcosConfigError(str(exc)) from exc
    data_mount = (data_volume.path if data_volume else DEFAULT_DATA_VOLUME_PATH).rstrip("/")
    if data_volume is None:  # the default: <hostname>-data
        block_volume: str | bool | None = None
        block_volume_size, block_volume_filesystem = DEFAULT_DATA_VOLUME_SIZE_GIB, DEFAULT_DATA_VOLUME_FILESYSTEM
    elif data_volume.digitalocean is None:  # a directory on the root disk
        block_volume = False
        block_volume_size, block_volume_filesystem = 0, DEFAULT_DATA_VOLUME_FILESYSTEM
    else:
        block_volume = data_volume.digitalocean.name
        block_volume_size = data_volume.digitalocean.size_gib
        block_volume_filesystem = data_volume.digitalocean.filesystem

    bind_raw = raw.get("bind_volumes")
    bind_volumes = None if bind_raw is None else _str_list(bind_raw, "bind_volumes")

    id_ranges = _int_map(raw.get("id_ranges"), "id_ranges", low=1, high=SUBUID_COUNT)
    for svc, start in id_ranges.items():
        if start + ID_RANGE_SIZE - 1 > SUBUID_COUNT:
            raise FcosConfigError(
                f"fcos.id_ranges.{svc}: {start} + {ID_RANGE_SIZE} IDs goes past the account's "
                f"{SUBUID_COUNT} subordinate IDs"
            )

    auto_update = raw.get("auto_update", False)
    if not isinstance(auto_update, bool):
        raise FcosConfigError("fcos.auto_update must be true or false")
    caddyfile = str(raw.get("caddyfile", "/etc/caddy/Caddyfile"))
    if not caddyfile.startswith("/"):
        raise FcosConfigError("fcos.caddyfile must be an absolute path inside the edge image")

    hostname = raw.get("hostname")
    if hostname is not None and not _HOSTNAME.fullmatch(str(hostname)):
        raise FcosConfigError(f"fcos.hostname must be a host name label (got {hostname!r})")
    keys = _public_keys(raw, "ssh_authorized_keys")
    admin_keys = _public_keys(raw, "admin_keys")

    return FcosSettings(
        user=user,
        uid=uid,
        data_mount=data_mount,
        bind_volumes=bind_volumes,
        volume_owners=_int_map(raw.get("volume_owners"), "volume_owners", low=0, high=4999),
        id_ranges=id_ranges,
        auto_update=auto_update,
        drop_ports=_str_list(raw.get("drop_ports"), "drop_ports"),
        caddyfile=caddyfile,
        hostname=str(hostname) if hostname is not None else None,
        timezone=str(raw["timezone"]) if raw.get("timezone") else None,
        block_volume=block_volume,
        block_volume_size=block_volume_size,
        block_volume_filesystem=block_volume_filesystem,
        ssh_authorized_keys=keys,
        admin_keys=admin_keys,
        update_window=_update_window(raw.get("update_window")),
        zabbix_image=str(raw.get("zabbix_image") or DEFAULT_ZABBIX_IMAGE),
    )
