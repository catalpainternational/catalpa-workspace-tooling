"""``dk <env> fcos diff|apply|secrets push|provision`` (issue #182, PR 3).

Plans are checked against the golden renders in ``tests/fixtures/fcos/*/expected``. The shell
the host runs (state and apply scripts) is run for real in a Fedora container when Docker is
available: apply onto an empty filesystem, read it back, expect no differences.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from catalpa_tooling.fcos import host as h

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"


def committed(name: str) -> h.Committed:
    return h.load_committed(FIXTURES / name / "expected")


def in_sync_state(c: h.Committed) -> h.HostState:
    """What a host that matches ``c`` reports."""
    s = h.HostState()
    for e in c.manifest["install"]:
        s.files[e["dest"]] = h.HostFile(int(e["mode"], 8), e["owner"], c.files[e["file"]])
    for v in c.manifest["bind_volumes"].values():
        s.dirs[v["path"]] = (int(v["mode"], 8), v["owner_uid"])
    for t in c.manifest["timers"]:
        s.links[f"{c.user_unit_dir}/timers.target.wants/{t}"] = f"../{t}"
    s.secrets = set(c.manifest["secrets"])
    s.system_secrets = set(c.manifest["system_secrets"])
    return s


def with_file(c: h.Committed, file: str, text: str) -> h.Committed:
    return replace(c, files={**c.files, file: text})


@pytest.mark.parametrize("name", ["site", "bero", "swlt"])
def test_a_matching_host_has_nothing_to_do(name: str) -> None:
    c = committed(name)
    plan = h.make_plan(c, in_sync_state(c))
    assert plan.files_in_sync and plan.secrets_in_sync, h.describe(plan)
    assert h.describe(plan) == []


def test_state_output_round_trips() -> None:
    text = "PORT=8000\n"
    out = "\n".join(
        [
            f"F\t/etc/site/django.env\t644\troot\t{base64.b64encode(text.encode()).decode()}",
            "M\t/etc/site/db.env",
            "D\t/var/mnt/data/pgdata\t700\t600822",
            "N\t/var/mnt/data/caddy_data",
            "L\t/x/timers.target.wants/a.timer\t../a.timer",
            "X\t/x/timers.target.wants/b.timer",
            "S\tzbx_tlspsk",
        ]
    )
    s = h.parse_state(out)
    assert s.files["/etc/site/django.env"] == h.HostFile(0o644, "root", text)
    assert s.files["/etc/site/db.env"] is None
    assert s.dirs == {"/var/mnt/data/pgdata": (0o700, 600822), "/var/mnt/data/caddy_data": None}
    assert s.links["/x/timers.target.wants/b.timer"] is None
    assert s.system_secrets == {"zbx_tlspsk"}


def test_changed_files_restart_only_their_long_running_units() -> None:
    c = committed("site")
    state = in_sync_state(c)
    state.files["/etc/site/migrate.env"] = h.HostFile(0o644, "root", "OLD=1\n")
    state.files["/etc/site/db.env"] = h.HostFile(0o600, "root", c.files["envfiles/db.env"])
    plan = h.make_plan(c, state)
    assert [(ch.dest, ch.why) for ch in plan.changes] == [
        ("/etc/site/db.env", "mode 600 → 644"),
        ("/etc/site/migrate.env", "changed"),
    ]
    assert any(line.startswith("    -OLD=1") for line in h.describe(plan))
    r = h.restarts_for(c, [ch.dest for ch in plan.changes])
    # migrate is PartOf django: restarting django reruns it.
    assert r.user == ["db.service", "django.service"]
    assert r.system == []


def test_quadlet_volume_and_socket_changes() -> None:
    c = committed("site")
    r = h.restarts_for(
        c,
        [
            f"{c.quadlet_dir}/caddy.container",
            f"{c.quadlet_dir}/django_static.volume",
            f"{c.user_unit_dir}/caddy.socket",
            f"{c.home}/.config/site/caddy.json",
        ],
    )
    assert r.user == ["caddy.service"]
    assert r.sockets == ["caddy"]
    assert any("django_static.volume" in n for n in r.notes)


def test_a_timer_driven_one_shot_is_never_restarted() -> None:
    c = committed("swlt")
    r = h.restarts_for(c, [], secrets=["restic_write_password", "postgres_password"])
    assert r.user == ["db.service"]
    assert h.long_running(c, "swlt-restic-files-backup") is None
    r = h.restarts_for(c, [f"{c.user_unit_dir}/swlt-restic-files-backup.timer"])
    assert r.user_timers == ["swlt-restic-files-backup.timer"]


def test_stack_units() -> None:
    assert h.stack_units(committed("site")) == (
        ["caddy.service", "db.service", "django.service"],
        [],
    )


def test_files_dropped_from_the_render_are_removed() -> None:
    c = committed("site")
    state = in_sync_state(c)
    before = json.loads(c.files["manifest.json"])
    before["install"].append(
        {"file": "quadlets/old.container", "dest": f"{c.quadlet_dir}/old.container"}
    )
    state.files[c.manifest_dest] = h.HostFile(0o644, "root", json.dumps(before))
    plan = h.make_plan(c, state)
    assert plan.remove == [f"{c.quadlet_dir}/old.container"]
    # The applied manifest differs too, so it is reinstalled.
    assert [ch.file for ch in plan.changes] == ["manifest.json"]
    assert h.user_reload_needed(c, plan)
    assert not h.system_reload_needed(c, plan)


def test_data_directories_are_made_but_never_chowned() -> None:
    c = committed("site")
    state = in_sync_state(c)
    state.dirs["/var/mnt/data/caddy_data"] = None
    state.dirs["/var/mnt/data/pgdata"] = (0o700, 1001)
    plan = h.make_plan(c, state)
    assert plan.make_dirs == [("/var/mnt/data/caddy_data", 0o700, 619823)]
    assert len(plan.dir_problems) == 1 and "pgdata" in plan.dir_problems[0]
    script = h.apply_script(c, plan)
    assert "install -d -m 700 -o 619823 -g 619823 /var/mnt/data/caddy_data" in script
    assert "pgdata" not in script


def test_missing_timer_links_are_enabled_and_started() -> None:
    c = committed("swlt")
    state = in_sync_state(c)
    link = f"{c.user_unit_dir}/timers.target.wants/swlt-pgbackrest-backup-full.timer"
    state.links[link] = None
    plan = h.make_plan(c, state)
    assert plan.links == [(link, "../swlt-pgbackrest-backup-full.timer")]
    _root, user = h.restart_commands(c, plan, h.Restarts(), restart=True)
    assert user.splitlines() == [
        "systemctl --user daemon-reload",
        "systemctl --user start swlt-pgbackrest-backup-full.timer",
    ]


def test_secret_names_are_compared_and_extras_left_alone() -> None:
    c = committed("site")
    state = in_sync_state(c)
    state.secrets = {"postgres_password", "leftover"}
    plan = h.make_plan(c, state)
    assert plan.files_in_sync and not plan.secrets_in_sync
    assert plan.secrets_missing == ["altcha_hmac_secret"]
    assert plan.secrets_extra == ["leftover"]


def test_secret_values() -> None:
    c = committed("site")
    values, problems = h.secret_values(
        c, {"postgres_password": "pw", "altcha_hmac_secret": "", "sops": {}}, None
    )
    assert values == [("postgres_password", "pw", False)]
    assert problems == ["altcha_hmac_secret: altcha_hmac_secret is empty (Podman refuses an empty secret)"]
    values, problems = h.secret_values(c, {}, ["nope"])
    assert problems == ["not in the manifest: nope"]


class FakeRunner:
    def __init__(self, replies: dict[str, bytes] | None = None):
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.replies = replies or {}

    def __call__(self, argv, *, input=None, tty=False):
        self.calls.append((argv, input))
        out = next((v for k, v in self.replies.items() if k in argv[-1]), b"")
        return subprocess.CompletedProcess(argv, 0, out, b"")


def test_secret_values_go_over_stdin_only() -> None:
    c = committed("site")
    runner = FakeRunner()
    host = h.Host("203.0.113.9", runner=runner)
    h.push_secrets(host, c, [("postgres_password", "s3cr3t", False), ("zbx", "psk", True)])
    (argv1, in1), (argv2, in2) = runner.calls
    assert argv1[-2:] == ["deploy@203.0.113.9", "podman secret create --replace postgres_password -"]
    assert argv2[-2:] == ["core@203.0.113.9", "sudo podman secret create --replace zbx -"]
    assert (in1, in2) == (b"s3cr3t", b"psk")
    assert not any("s3cr3t" in a for a in argv1 + argv2)


def test_host_address_from_docker_host() -> None:
    host = h.host_from_docker_host("ssh://deploy@example.org/run/user/1001/podman/podman.sock", None)
    assert (host.address, host.port) == ("example.org", 22)
    assert h.host_from_docker_host(None, "203.0.113.9").address == "203.0.113.9"
    with pytest.raises(h.HostError):
        h.host_from_docker_host(None, None)


def test_private_registries() -> None:
    assert h.private_registries(committed("site")) == ["ghcr.io"]


def test_host_commands_refuse_an_uncommitted_render(monkeypatch, tmp_path) -> None:
    """The fixtures' env has no committed generated/fcos/: nothing may reach SSH."""
    import argparse

    from catalpa_tooling.config import load_project_config
    from catalpa_tooling.fcos import cli
    from catalpa_tooling.fcos.render import Render

    config = load_project_config(FIXTURES / "site")
    fake = Render(files={"manifest.json": "{}\n"}, compose_file="compose.yml", env={}, secret_keys=frozenset())
    monkeypatch.setattr(cli, "render_env", lambda *a, **k: fake)
    monkeypatch.setattr(cli, "required_secret_problems", lambda *a, **k: [])
    monkeypatch.setattr(h, "run", lambda *a, **k: pytest.fail("ssh was called"))
    ns = argparse.Namespace(fcos_command="apply", host="203.0.113.9", dry_run=False, yes=True)
    assert cli.handle_fcos_command(ns, config, "staging") == 1


# --- The host's shell, for real ------------------------------------------------------------

FEDORA = "quay.io/fedora/fedora:41"


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker")
@pytest.mark.parametrize("name", ["site", "swlt"])
def test_apply_then_state_in_fedora(name: str) -> None:
    c = committed(name)
    empty = h.HostState()
    plan = h.make_plan(c, empty)
    assert plan.make_dirs and plan.changes
    marker = "=====STATE====="
    script = (
        "set -e\nmkdir -p /var/home /var/mnt/data\n"
        f"useradd -u {c.uid} -m -d {c.home} {c.user} >/dev/null 2>&1\n"
        + h.apply_script(c, plan)
        # Run it twice: applying is idempotent.
        + h.apply_script(c, plan)
        + f"echo {marker}\n"
        + h.state_script(c)
        + f"stat -c '%U %a' {c.user_unit_dir} {c.home}/.config\n"
    )
    r = subprocess.run(
        ["docker", "run", "--rm", "-i", FEDORA, "sh", "-s"],
        input=script.encode(), capture_output=True, timeout=600, check=False,
    )  # fmt: skip
    if r.returncode != 0 and b"Cannot connect to the Docker daemon" in r.stderr:
        pytest.skip("no Docker daemon")
    assert r.returncode == 0, r.stderr.decode()
    out = r.stdout.decode().split(marker + "\n", 1)[1]
    state = h.parse_state(out)
    state.secrets = set(c.manifest["secrets"])
    after = h.make_plan(c, state)
    assert after.files_in_sync, h.describe(after, show_diff=False)
    assert after.dir_problems == []
    # The account's directories are its own (systemd --user must be able to read them).
    assert out.strip().splitlines()[-2:] == ["deploy 755", "deploy 755"]
