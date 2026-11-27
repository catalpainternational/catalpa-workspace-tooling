"""``dk <env> host``'s SSH section (#207): parsers on sample text, hosts through a fake runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from catalpa_tooling import host_state as hs
from catalpa_tooling.fcos import host as h
from catalpa_tooling.fcos.host import Host

FIXTURES = Path(__file__).parent / "fixtures" / "fcos"


def levels(checks) -> dict[str, str]:
    return {c.label: c.level for c in checks}


def test_ostree_booted_and_nothing_staged() -> None:
    text = json.dumps({"deployments": [{"booted": True, "version": "45.20260101.3.0"}]})
    c = hs.parse_ostree(text)
    assert [(x.label, x.level) for x in c] == [("OS", "ok"), ("update", "ok")]
    assert "45.20260101.3.0" in c[0].detail


def test_ostree_staged_update_is_a_warning() -> None:
    text = json.dumps(
        {
            "deployments": [
                {"staged": True, "version": "46.20260201.3.0"},
                {"booted": True, "version": "45.20260101.3.0"},
            ]
        }
    )
    c = {x.label: x for x in hs.parse_ostree(text)}
    assert c["update"].level == "warn" and "46.20260201.3.0" in c["update"].detail
    assert "45.20260101.3.0" in c["OS"].detail


def test_ostree_unreadable() -> None:
    assert hs.parse_ostree("error: not found")[0].level == "warn"


def test_zincati_active_and_not() -> None:
    assert hs.parse_zincati("active\nUpdate staged").level == "ok"
    bad = hs.parse_zincati("inactive")
    assert bad.level == "warn" and "inactive" in bad.detail


def test_failed_units() -> None:
    assert hs.parse_failed("", "failed system units").level == "ok"
    bad = hs.parse_failed("django.service loaded failed failed catalpa django\n", "failed units (deploy)")
    assert bad.level == "FAIL" and bad.detail == "django.service"


def test_mount_and_linger() -> None:
    assert hs.parse_mount("mounted", "/var/mnt/data").level == "ok"
    assert hs.parse_mount("not-mounted", "/var/mnt/data").level == "FAIL"
    assert hs.parse_linger("Linger=yes", "deploy").level == "ok"
    assert hs.parse_linger("Linger=no", "deploy").level == "FAIL"


def test_disk() -> None:
    assert hs.parse_disk("/dev/vda1 100 40 60 40% /").level == "ok"
    assert hs.parse_disk("/dev/vda1 100 90 10 90% /").level == "warn"
    assert hs.parse_disk("/dev/vda1 100 97 3 97% /").level == "FAIL"
    assert hs.parse_disk("").level == "warn"


def test_data_mount_comes_from_the_bind_volumes() -> None:
    c = h.load_committed(FIXTURES / "bero" / "expected")
    assert hs.data_mount_of(c.manifest) == "/var/mnt/data"
    assert hs.data_mount_of({}) is None


ROOT_OUT = (
    "== ostree\n"
    + json.dumps({"deployments": [{"booted": True, "version": "45.1"}]})
    + "\n== zincati\nactive\nChecked for updates\n"
    "== boots\n-1 aaa Tue 2026-11-24 — Wed 2026-11-25\n 0 bbb Wed 2026-11-25 — Thu 2026-11-26\n"
    "== failed\n"
    "== mount\nmounted\n"
    "== linger\nLinger=yes\n"
)


def fake_host(root_out: str = ROOT_OUT, user_failed: str = "", user_rc: int = 0, root_fails: bool = False) -> Host:
    def runner(argv, input=None, **_k):
        command = argv[-1]
        if command.startswith("sudo sh -s"):
            if root_fails:
                return subprocess.CompletedProcess(argv, 255, b"", b"Permission denied (publickey)")
            return subprocess.CompletedProcess(argv, 0, root_out.encode(), b"")
        if "--user --failed" in command:
            return subprocess.CompletedProcess(argv, user_rc, user_failed.encode(), b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    return Host(address="203.0.113.7", runner=runner)


def test_rendered_host_all_good() -> None:
    checks = hs.rendered_state(fake_host(), user="deploy", data_mount="/var/mnt/data")
    assert set(levels(checks).values()) == {"ok"}
    assert {"OS", "update", "zincati", "boots", "failed system units", "data volume", "linger"} <= set(levels(checks))


def test_rendered_host_failed_user_unit_fails_the_run() -> None:
    checks = hs.rendered_state(
        fake_host(user_failed="django.service loaded failed failed x\n"), user="deploy", data_mount="/var/mnt/data"
    )
    assert levels(checks)["failed units (deploy)"] == "FAIL"
    assert hs.print_state("staging", "203.0.113.7", checks) == 1


def test_rendered_host_without_admin_access_says_so_and_still_reads_the_account() -> None:
    checks = hs.rendered_state(fake_host(root_fails=True), user="deploy", data_mount=None)
    lv = levels(checks)
    assert lv["machine"] == "FAIL"
    assert lv["failed units (deploy)"] == "ok"


def test_staged_update_alone_does_not_fail_the_run(capsys) -> None:
    out = ROOT_OUT.replace(
        json.dumps({"deployments": [{"booted": True, "version": "45.1"}]}),
        json.dumps({"deployments": [{"staged": True, "version": "46.0"}, {"booted": True, "version": "45.1"}]}),
    )
    checks = hs.rendered_state(fake_host(out), user="deploy", data_mount="/var/mnt/data")
    assert hs.print_state("staging", "203.0.113.7", checks) == 0
    assert "46.0 is staged" in capsys.readouterr().err


def test_compose_host() -> None:
    out = (
        "== uptime\nup 3 days\n== reboot\nrequired\n== failed\n== disk\n/dev/vda1 100 40 60 40% /\n"
    )

    def runner(argv, input=None, **_k):
        return subprocess.CompletedProcess(argv, 0, out.encode(), b"")

    checks = hs.compose_state(Host(address="203.0.113.7", runner=runner), login="root")
    lv = levels(checks)
    assert lv["reboot"] == "warn" and lv["failed system units"] == "ok" and lv["disk /"] == "ok"
    assert hs.print_state("prod", "203.0.113.7", checks) == 0


def test_compose_host_unreachable() -> None:
    def runner(argv, input=None, **_k):
        return subprocess.CompletedProcess(argv, 255, b"", b"Connection timed out")

    checks = hs.compose_state(Host(address="203.0.113.7", runner=runner), login="root")
    assert checks[0].level == "FAIL" and "timed out" in checks[0].detail


def test_docker_host_login() -> None:
    assert hs.docker_host_login("ssh://deploy@host.example/run/user/1001/podman/podman.sock") == "deploy"
    assert hs.docker_host_login("ssh://host.example") is None
