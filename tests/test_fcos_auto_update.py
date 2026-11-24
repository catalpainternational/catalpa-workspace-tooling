"""``fcos.auto_update`` drop-ins: pull every image first, and ``fcos.auto_update_at`` (#213)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from catalpa_tooling.fcos import host as h
from catalpa_tooling.fcos.auto_update import (
    SERVICE_DROPIN,
    TIMER_DROPIN,
    auto_update_files,
    auto_update_images,
)
from catalpa_tooling.fcos.settings import FcosConfigError, FcosSettings, parse_fcos_settings

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"
S = FcosSettings(auto_update=True)


def quadlet(image: str, *, auto: bool = True) -> str:
    return f"[Container]\nImage={image}\n" + ("AutoUpdate=registry\n" if auto else "")


def entry(name: str, dest_dir: str) -> dict:
    return {"file": f"quadlets/{name}.container", "dest": f"{dest_dir}/{name}.container"}


def test_images_are_the_accounts_auto_updated_quadlets_once_each() -> None:
    files = {
        "quadlets/django.container": quadlet("ghcr.io/x/django:staging"),
        "quadlets/migrate.container": quadlet("ghcr.io/x/django:staging"),
        "quadlets/db.container": quadlet("ghcr.io/x/db:staging"),
        "quadlets/oneoff.container": quadlet("ghcr.io/x/tool:1", auto=False),
        "quadlets/zabbix.container": quadlet("docker.io/zabbix/agent:7"),
    }
    install = [entry(n, S.quadlet_dir) for n in ("django", "migrate", "db", "oneoff")]
    install.append(entry("zabbix", "/etc/containers/systemd"))  # a system quadlet: root's
    assert auto_update_images(S, files, install) == ["ghcr.io/x/db:staging", "ghcr.io/x/django:staging"]


def test_the_service_drop_in_pulls_first_and_the_timer_drop_in_needs_a_time() -> None:
    files = {"quadlets/django.container": quadlet("ghcr.io/x/django:staging")}
    install = [entry("django", S.quadlet_dir)]
    out, inst = auto_update_files(S, "# hdr\n", files, install)
    assert set(out) == {f"systemd/{SERVICE_DROPIN}"}
    assert "ExecStartPre=/usr/bin/podman pull --quiet ghcr.io/x/django:staging" in out[f"systemd/{SERVICE_DROPIN}"]
    assert inst == [
        {
            "file": f"systemd/{SERVICE_DROPIN}",
            "dest": f"{S.user_unit_dir}/{SERVICE_DROPIN}",
            "owner": "deploy",
            "mode": "0644",
        }
    ]

    out, _ = auto_update_files(replace(S, auto_update_at="13:00"), "", files, install)
    timer = out[f"systemd/{TIMER_DROPIN}"]
    # The empty OnCalendar= clears Podman's daily midnight before setting the time.
    assert "[Timer]\nOnCalendar=\nOnCalendar=*-*-* 13:00:00\nRandomizedDelaySec=0\n" in timer


def test_nothing_without_auto_update() -> None:
    files = {"quadlets/django.container": quadlet("ghcr.io/x/django:1")}
    assert auto_update_files(FcosSettings(), "", files, [entry("django", S.quadlet_dir)]) == ({}, [])


@pytest.mark.parametrize(
    "raw,message",
    [
        ({"auto_update": True, "auto_update_at": "1pm"}, "HH:MM"),
        ({"auto_update": True, "auto_update_at": "24:00"}, "HH:MM"),
        ({"auto_update_at": "13:00"}, "needs fcos.auto_update: true"),
    ],
)
def test_auto_update_at_is_checked(raw: dict, message: str) -> None:
    with pytest.raises(FcosConfigError, match=message):
        parse_fcos_settings({"runtime": "quadlet", "fcos": raw})


def test_auto_update_at_parses() -> None:
    s = parse_fcos_settings({"runtime": "quadlet", "fcos": {"auto_update": True, "auto_update_at": "07:30"}})
    assert s.auto_update_at == "07:30"


def test_a_changed_schedule_restarts_the_timer_and_a_pull_drop_in_restarts_nothing() -> None:
    c = h.load_committed(FIXTURES / "bero" / "expected")
    timer_dropin = f"{c.user_unit_dir}/{TIMER_DROPIN}"
    service_dropin = f"{c.user_unit_dir}/{SERVICE_DROPIN}"
    r = h.restarts_for(c, [timer_dropin, service_dropin])
    assert r.user_timers == ["podman-auto-update.timer"]
    assert r.user == [] and r.system == []
