"""``monitoring:`` in an env's ``info.yaml``: whether the env's host is monitored.

Off unless the env says so, as on compose hosts, where nothing is monitored until someone runs
``dk <env> zabbix install``. A rendered host (``runtime: quadlet``) gets the Zabbix agent, the
read-only Podman proxy and the backup status files only with ``monitoring: zabbix``.
``ops.zabbix`` in ``tooling.yaml`` stays the project's settings for it (unit and file names).
"""

from __future__ import annotations

from typing import Any

#: The values ``monitoring:`` takes, besides false or leaving it out.
MONITORING_KINDS = ("zabbix",)


class MonitoringSettingError(ValueError):
    pass


def monitoring_of(info: dict[str, Any]) -> str | None:
    """``"zabbix"``, or None when the env isn't monitored (``monitoring:`` false or absent)."""
    raw = info.get("monitoring")
    if raw is None or raw is False:
        return None
    if isinstance(raw, str) and raw.strip() in MONITORING_KINDS:
        return raw.strip()
    raise MonitoringSettingError(
        f"monitoring: {raw!r} isn't a value it takes: use `monitoring: zabbix`, or false (the "
        "default) for no monitoring"
    )
