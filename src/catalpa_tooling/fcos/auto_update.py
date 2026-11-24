"""``fcos.auto_update``: drop-ins for the account's ``podman-auto-update`` service and timer.

``podman auto-update`` handles one unit at a time: it pulls that unit's image, then restarts it.
A unit another one depends on (``db``) takes the app down with it, and the app then stays down
while its own image downloads (#213: 1 min 37 s on a test host, 64 s of it one pull). So the
service pulls every image first (``ExecStartPre``); auto-update then finds them local, and each
restart is only a restart. A failed pull fails the run before anything restarts.

``fcos.auto_update_at`` moves the run from Podman's default (daily at midnight, up to 15 minutes
late) to a fixed time of day in the host's time zone, without the random delay, so someone can
watch it.
"""

from __future__ import annotations

import re

from catalpa_tooling.fcos.settings import FcosSettings

SERVICE_DROPIN = "podman-auto-update.service.d/10-dk-pull-first.conf"
TIMER_DROPIN = "podman-auto-update.timer.d/10-dk-schedule.conf"


def auto_update_images(settings: FcosSettings, files: dict[str, str], install: list[dict]) -> list[str]:
    """Images of the account's quadlets marked ``AutoUpdate=registry``, sorted, once each."""
    images: set[str] = set()
    for e in install:
        if not (e["file"].startswith("quadlets/") and e["file"].endswith(".container")):
            continue
        if not e["dest"].startswith(settings.quadlet_dir + "/"):
            continue  # a system quadlet (the Zabbix agent): root's auto-update, not the account's
        text = files[e["file"]]
        if not re.search(r"^AutoUpdate=registry\s*$", text, re.M):
            continue
        m = re.search(r"^Image=(\S+)", text, re.M)
        if m:
            images.add(m.group(1))
    return sorted(images)


def auto_update_files(
    settings: FcosSettings, header: str, files: dict[str, str], install: list[dict]
) -> tuple[dict[str, str], list[dict]]:
    """(files, install entries) for the drop-ins; none without ``auto_update``."""
    if not settings.auto_update:
        return {}, []
    out_files: dict[str, str] = {}
    out_install: list[dict] = []

    def add(rel: str, text: str) -> None:
        path = f"systemd/{rel}"
        out_files[path] = header + text
        out_install.append(
            {"file": path, "dest": f"{settings.user_unit_dir}/{rel}", "owner": settings.user, "mode": "0644"}
        )

    images = auto_update_images(settings, files, install)
    if images:
        add(
            SERVICE_DROPIN,
            "# Pull every image auto-update may switch to before it restarts anything, so a unit\n"
            "# isn't down while its image downloads. A failed pull fails the run: nothing restarts.\n"
            "[Service]\n" + "".join(f"ExecStartPre=/usr/bin/podman pull --quiet {img}\n" for img in images),
        )
    if settings.auto_update_at:
        add(
            TIMER_DROPIN,
            f"# fcos.auto_update_at: daily at {settings.auto_update_at}, the host's time zone, on time.\n"
            "[Timer]\n"
            "OnCalendar=\n"
            f"OnCalendar=*-*-* {settings.auto_update_at}:00\n"
            "RandomizedDelaySec=0\n",
        )
    return out_files, out_install
