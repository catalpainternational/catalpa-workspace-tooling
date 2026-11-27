"""A blue/green site in three accounts (site, blue, green) on a shared server.

The app is ``tests/fixtures/fcos/bluegreen-app`` (Partisipa-shaped: v1, v2 and a v2 whose
migration fails). The deploy runs against a fake server that keeps the site account's state
files and answers each command the way the real ones do.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from catalpa_tooling.fcos import bluegreen as bg
from catalpa_tooling.fcos.shared_host import load_host
from tests.test_fcos_shared_host import ADMIN, DK

REPO = Path(__file__).resolve().parents[1]
APP = "tests/fixtures/fcos/bluegreen-app"


def _host(tmp_path: Path):
    d = tmp_path / "shared-1"
    d.mkdir()
    (d / "host.yaml").write_text(
        yaml.safe_dump(
            {
                "admin_keys": [ADMIN],
                "ssh_authorized_keys": [DK],
                "accounts": [
                    {"name": "deploy", "slot": 1, "project": "nextgen", "env": "staging", "names": ["n.test"]},
                    {"name": "site", "slot": 2, "project": "partisipa", "env": "staging", "role": "site", "names": ["p.test"]},
                    {"name": "blue", "slot": 3, "project": "partisipa", "env": "staging", "role": "blue"},
                    {"name": "green", "slot": 4, "project": "partisipa", "env": "staging", "role": "green"},
                ],
            }
        )
    )  # fmt: skip
    return load_host(d)


def _render(tmp_path, site="v1", blue="v1", green="v2"):
    info = {"bluegreen": {"units": APP, "site": {"path": site}, "blue": {"path": blue}, "green": {"path": green}}}
    return bg.render_bluegreen("partisipa", "staging", bg.parse_bluegreen(info), _host(tmp_path), REPO)


def test_each_colour_runs_its_own_version_of_the_same_units(tmp_path):
    files = _render(tmp_path)
    blue, green = files["blue/units/notes-web@.container"], files["green/units/notes-web@.container"]
    assert "UNITS_VERSION=1" in blue and "UNITS_VERSION=2" in green
    mb = json.loads(files["blue/manifest.json"])
    mg = json.loads(files["green/manifest.json"])
    assert (mb["units_version"], mg["units_version"]) == ("1", "2")
    # Same file names, each in its own account's folder.
    assert {e["dest"] for e in mb["install"]} & {e["dest"] for e in mg["install"]} == set()
    assert "/etc/containers/systemd/users/1003/notes-web@.container" in {e["dest"] for e in mb["install"]}
    assert "/etc/containers/systemd/users/1004/notes-web@.container" in {e["dest"] for e in mg["install"]}


def test_ports_come_from_the_slots_and_reset_the_network(tmp_path):
    files = _render(tmp_path)
    site = files["site/units/notes-site@.pod.d/50-dk.conf"]
    assert "[Pod]\nNetwork=\nNetwork=pasta:--map-host-loopback,169.254.1.3\n" in site
    assert "PublishPort=127.0.0.1:20020:80" in site and "PublishPort=127.0.0.1:20021:5432" in site
    assert "PublishPort=127.0.0.1:20030:8000" in files["blue/units/notes@.pod.d/50-dk.conf"]
    assert "PublishPort=127.0.0.1:20040:8000" in files["green/units/notes@.pod.d/50-dk.conf"]
    assert json.loads(files["site/manifest.json"])["ports"] == {"edge": 20020, "db": 20021}


def test_plain_systemd_units_go_to_the_accounts_systemd_folder(tmp_path):
    m = json.loads(_render(tmp_path)["green/manifest.json"])
    dests = {e["file"]: e["dest"] for e in m["install"]}
    assert dests["units/notes@.target"] == "/var/home/green/.config/systemd/user/notes@.target"
    assert dests["units/notes@.pod"] == "/etc/containers/systemd/users/1004/notes@.pod"
    assert m["start"] == ["notes@staging-green.target"]


def test_secrets_are_named_per_account_and_mounted_by_the_units(tmp_path):
    files = _render(tmp_path)
    assert json.loads(files["site/manifest.json"])["secrets"] == {"notes-staging-postgres-password": "postgres-password"}
    assert json.loads(files["blue/manifest.json"])["secrets"] == {"notes-staging-blue-dotenv": "dotenv"}
    # dk adds no Secret= lines: the app's units mount their own.
    assert not any("Secret=" in text for p, text in files.items() if p.endswith("50-dk.conf"))


def test_the_hooks_scripts_come_from_the_sites_version(tmp_path):
    m = json.loads(_render(tmp_path)["site/manifest.json"])
    bins = {e["dest"]: e["mode"] for e in m["install"] if "/bin/" in e["dest"]}
    assert bins == {
        "/etc/dk/partisipa/staging/site/bin/snapshot-db": "0755",
        "/etc/dk/partisipa/staging/site/bin/switch-site": "0755",
    }
    assert m["hooks"]["switch"] == "bin/switch-site {env} {colour} {port}"


def test_a_version_can_come_from_a_git_ref(tmp_path):
    repo = tmp_path / "repo"
    app = repo / "app"
    app.parent.mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    import shutil

    shutil.copytree(REPO / APP / "v1", app)
    git("init", "-q")
    git("-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1")
    git("tag", "v1")
    shutil.rmtree(app)
    shutil.copytree(REPO / APP / "v2", app)  # the working tree moves on to v2
    info = {"bluegreen": {"units": "app", "site": {"ref": "v1"}, "blue": {"ref": "v1"}, "green": {}}}
    files = bg.render_bluegreen("partisipa", "staging", bg.parse_bluegreen(info), _host(tmp_path), repo)
    assert "UNITS_VERSION=1" in files["blue/units/notes-web@.container"]
    assert "UNITS_VERSION=2" in files["green/units/notes-web@.container"]
    assert json.loads(files["blue/manifest.json"])["source"] == {"ref": "v1", "path": "."}


def test_the_env_needs_site_blue_and_green_accounts(tmp_path):
    info = {"bluegreen": {"units": APP, "site": {"path": "v1"}, "blue": {"path": "v1"}, "green": {"path": "v2"}}}
    with pytest.raises(bg.BlueGreenError, match="first boot"):
        bg.render_bluegreen("partisipa", "production", bg.parse_bluegreen(info), _host(tmp_path), REPO)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda t: t.replace('edge = "site:notes-site@.pod:80"\n', ""), "ports.edge"),
        (lambda t: t.replace('colour = "colour:notes@.pod:8000"', 'colour = "colour:nope@.pod:8000"'), "isn't one of"),
        (lambda t: t.replace("thaw = ", "thawed = "), "unknown hooks"),
        (lambda t: t.replace('set = "notes"', ""), "set"),
    ],
)
def test_app_toml_is_checked(change, message):
    text = (REPO / APP / "v1" / "app.toml").read_text()
    with pytest.raises(bg.BlueGreenError, match=message):
        bg.parse_app_toml(change(text), "app.toml")


# --- the deploy, against a fake server ----------------------------------------------------------


class FakeServer:
    """Each account's commands: the site account's state files, hooks that pass or fail as set,
    colours whose units start or fail as set. ``calls`` keeps (account, what) in order."""

    def __init__(self, files):
        self.manifests = {r: json.loads(files[f"{r}/manifest.json"]) for r in bg.ROLES}
        self.state: dict[str, str] = {}
        self.hook_rc: dict[str, int] = {}
        self.fails_to_start: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.links: dict[str, set[str]] = {r: set() for r in bg.ROLES}
        self.locked = False

    def ssh(self, login, command, *, input=None, check=True):
        role = next(r for r, m in self.manifests.items() if m["user"] == login)
        out, rc = "", 0
        sd = ".local/state/dk/staging/"
        if command.endswith(f"mkdir {sd}lock"):
            rc = 1 if self.locked else 0
            self.locked = True
        elif command.startswith(f"rmdir {sd}lock"):
            self.locked = False
        elif command.startswith(f"cat {sd}"):
            name = command.split(sd)[1].split()[0]
            rc, out = (0, self.state[name] + "\n") if name in self.state else (1, "")
        elif f"> {sd}" in command:
            name = command.rsplit(sd, 1)[1]
            self.state[name] = command.split("printf '%s\\n' ", 1)[1].rsplit(" > ", 1)[0].strip("'")
        elif command.startswith(f"rm -f {sd}"):
            self.state.pop(command.split(sd)[1], None)
        elif command.startswith("cd /etc/dk/"):
            words = command.split("bin/", 1)[1].split()
            if words[0] == "snapshot-db":  # snapshot-db <env> <what> [snapshot]
                name, args = words[2], [words[1], *words[3:]]
            else:  # switch-site <env> <colour> <port>
                name, args = "switch", words[1:]
            name = {"take-frozen": "done", "restore": "rollback"}.get(name, name)
            self.calls.append((role, f"hook {name} {' '.join(args)}"))
            rc = self.hook_rc.get(name, 1 if name == "frozen" else 0)
            if name == "done" and rc == 0:
                out = "/snapshots/staging-1.dump\n"
        elif "systemctl --user start" in command:
            self.calls.append((role, "start"))
        elif "systemctl --user stop" in command:
            assert "list-dependencies" in command, "stop waits for what the target wants"
            self.calls.append((role, "stop"))
        elif "list-dependencies" in command:
            out = f"x {'failed' if role in self.fails_to_start else 'active'}\n"
        elif command.startswith("curl"):
            port = int(command.split("127.0.0.1:")[1].split("/")[0])
            colour = next(r for r in bg.COLOURS if self.manifests[r]["ports"]["colour"] == port)
            rc = 1 if colour in self.fails_to_start else 0
        elif command.startswith("podman secret ls"):
            out = "\n".join(self.manifests[role]["secrets"]) + "\n"
        elif "default.target.wants" in command:
            (self.links[role].add if command.startswith("mkdir") else self.links[role].discard)("wanted")
            if role == "site":
                assert "notes-site@staging.target" in command
        if check and rc != 0:
            raise AssertionError(f"{login}: {command} -> {rc}")
        return subprocess.CompletedProcess([], rc, out.encode(), b"")


@pytest.fixture
def site(tmp_path, monkeypatch):
    files = _render(tmp_path)
    server = FakeServer(files)
    monkeypatch.setattr(bg, "install", lambda site, role: False)
    monkeypatch.setattr(bg, "pull", lambda site, role: None)
    s = bg.Site(
        env_name="staging",
        manifests=server.manifests,
        committed={},
        target=server,
        say=lambda m: None,
        sleep=lambda s: None,
    )
    return s, server


def _steps(server):
    return [c for c in server.calls if c != ("site", "start")]


def test_first_deploy_goes_to_blue_without_freezing(site):
    s, server = site
    assert bg.deploy(s) == 0
    assert server.state["live"] == "blue"
    assert "mark" not in server.state and "rollback" not in server.state
    assert _steps(server) == [("blue", "start"), ("site", "hook switch staging blue 20030")]
    assert server.links == {"site": {"wanted"}, "blue": {"wanted"}, "green": set()}


def test_deploy_stops_live_freezes_starts_switches_in_order(site):
    s, server = site
    server.state["live"] = "blue"
    assert bg.deploy(s) == 0
    assert _steps(server) == [
        ("site", "hook frozen staging"),
        ("blue", "stop"),
        ("site", "hook freeze staging"),
        ("green", "start"),
        ("site", "hook switch staging green 20040"),
        ("site", "hook done staging"),
    ]
    assert server.state["live"] == "green"
    assert server.state["rollback"] == "blue /snapshots/staging-1.dump"
    assert "mark" not in server.state
    assert server.links["green"] == {"wanted"} and server.links["blue"] == set()


def test_a_colour_that_fails_to_start_puts_the_old_one_back(site):
    s, server = site
    server.state["live"] = "blue"
    server.fails_to_start.add("green")
    assert bg.deploy(s) == 1
    assert _steps(server)[-5:] == [
        ("green", "start"),
        ("green", "stop"),
        ("site", "hook thaw staging"),
        ("blue", "start"),
        ("site", "hook switch staging blue 20030"),
    ]
    assert server.state["live"] == "blue"
    assert "mark" not in server.state and "rollback" not in server.state


def test_a_failed_put_back_leaves_the_mark_and_refuses_later_deploys(site):
    s, server = site
    server.state["live"] = "blue"
    server.fails_to_start.add("green")
    server.hook_rc["thaw"] = 1
    assert bg.deploy(s) == 1
    assert server.state["mark"].startswith("deploy green from blue")
    server.fails_to_start.clear()
    server.calls.clear()
    assert bg.deploy(s) == 1
    assert _steps(server) == []  # refused before touching anything


def test_a_freeze_that_cannot_run_stops_nothing(site):
    s, server = site
    server.state["live"] = "blue"
    server.hook_rc["frozen"] = 2
    assert bg.deploy(s) == 4
    assert _steps(server) == [("site", "hook frozen staging")]
    assert "mark" not in server.state


def test_a_database_left_aside_is_kept_before_the_next_freeze(site):
    s, server = site
    server.state["live"] = "blue"
    server.hook_rc["frozen"] = 0
    assert bg.deploy(s) == 0
    assert _steps(server)[:2] == [("site", "hook frozen staging"), ("site", "hook done staging")]


def test_one_deploy_at_a_time(site):
    s, server = site
    server.locked = True
    assert bg.deploy(s) == 1
    assert server.calls == []


def test_rollback_restores_the_snapshot_and_the_previous_colour(site):
    s, server = site
    server.state["live"] = "blue"
    assert bg.deploy(s) == 0
    server.calls.clear()
    assert bg.rollback(s) == 0
    assert _steps(server) == [
        ("site", "hook keep staging"),
        ("green", "stop"),
        ("site", "hook rollback staging /snapshots/staging-1.dump"),
        ("blue", "start"),
        ("site", "hook switch staging blue 20030"),
    ]
    assert server.state["live"] == "blue"
    assert "rollback" not in server.state and "mark" not in server.state
    assert bg.rollback(s) == 1  # one step back only


def test_a_colour_starts_at_boot_once_the_sites_ports_answer(tmp_path):
    files = _render(tmp_path)
    unit = files["green/units/dk-colour-boot.service"]
    assert "/dev/tcp/127.0.0.1/$p" in unit and "for p in 20020 20021;" in unit
    assert "systemctl --user start --no-block notes@staging-green.target" in unit
    dests = {e["dest"] for e in json.loads(files["green/manifest.json"])["install"]}
    assert "/var/home/green/.config/systemd/user/dk-colour-boot.service" in dests


def test_a_rollback_whose_keep_fails_changes_nothing(site):
    s, server = site
    server.state["live"] = "blue"
    assert bg.deploy(s) == 0
    server.calls.clear()
    server.hook_rc["keep"] = 1
    assert bg.rollback(s) == 1
    assert _steps(server) == [("site", "hook keep staging")]
    assert server.state["live"] == "green" and "mark" not in server.state


def test_rollback_after_a_failed_put_back_stops_the_colour_that_was_deploying(site):
    s, server = site
    server.state["live"] = "blue"
    server.fails_to_start.add("green")
    server.hook_rc["thaw"] = 1
    assert bg.deploy(s) == 1
    server.fails_to_start.clear()
    server.hook_rc.update(thaw=0, frozen=0)
    server.calls.clear()
    assert bg.rollback(s) == 0
    assert _steps(server)[:3] == [("site", "hook keep staging"), ("green", "stop"), ("blue", "stop")]


@pytest.mark.parametrize("name", ["../1001/evil.container", "/etc/sudoers.d/x", ".hidden.container"])
def test_unit_names_cannot_leave_the_accounts_folder(name):
    text = (REPO / APP / "v1" / "app.toml").read_text().replace('"notes-db@.container"', f'"{name}"', 1)
    with pytest.raises(bg.BlueGreenError, match="isn't a plain file name"):
        bg.parse_app_toml(text, "app.toml")


@pytest.mark.parametrize(
    ("hook", "template"),
    [
        ("freeze", "bin/snapshot-db {env} freeze ${HOME}"),  # a shell variable, not dk's
        ("freeze", "bin/snapshot-db {env} freeze {colour}"),  # switch's field, not freeze's
        ("done", "bin/snapshot-db {env} take-frozen {snapshot}"),  # rollback's
        ("freeze", "bin/snapshot-db {env.x} freeze"),
        ("freeze", "bin/snapshot-db {env[x]} freeze"),
    ],
)
def test_a_hook_with_a_brace_dk_does_not_fill_there_is_refused_before_any_deploy(hook, template):
    text = (REPO / APP / "v1" / "app.toml").read_text()
    line = next(x for x in text.splitlines() if x.startswith(f"{hook} = "))
    with pytest.raises(bg.BlueGreenError, match=f"hooks.{hook}"):
        bg.parse_app_toml(text.replace(line, f'{hook} = "{template}"'), "app.toml")


def test_each_hook_may_use_its_own_fields():
    app = bg.parse_app_toml((REPO / APP / "v1" / "app.toml").read_text(), "app.toml")
    assert "{port}" in app.hooks["switch"] and "{snapshot}" in app.hooks["rollback"]


@pytest.mark.parametrize("units", ["/etc", "../../etc"])
def test_units_must_be_a_folder_in_the_repo(units):
    with pytest.raises(bg.BlueGreenError, match="relative"):
        bg.parse_bluegreen({"bluegreen": {"units": units}})


def test_hook_scripts_are_plain_file_names(tmp_path, monkeypatch):
    real = bg.Version.listdir
    monkeypatch.setattr(bg.Version, "listdir", lambda self, rel: ["../../sudoers"] if rel == "bin" else real(self, rel))
    with pytest.raises(bg.BlueGreenError, match="isn't a plain file name"):
        _render(tmp_path)


def test_the_rollback_record_is_written_before_the_mark(site):
    s, server = site
    server.state["live"] = "blue"
    order = []
    real = s.write_state
    s.write_state = lambda name, value: (order.append(name), real(name, value))[1]
    assert bg.deploy(s) == 0
    assert order[:2] == ["rollback", "mark"]


def test_bin_at_a_ref_lists_files_not_folders(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app" / "bin" / "lib").mkdir(parents=True)
    (repo / "app" / "bin" / "switch-site").write_text("#!/bin/sh\n")
    (repo / "app" / "bin" / "lib" / "x").write_text("x\n")
    for args in (["init", "-q"], ["add", "."], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    v = bg.Version(repo, "app", "HEAD", bg._git(repo))
    assert v.listdir("bin") == ["switch-site"]


@pytest.mark.parametrize(
    ("keep", "printed", "note"),
    [
        (True, "/kept/1.dump\n", "(kept in /kept/1.dump)"),
        (True, "", "(the keep hook kept them)"),
        (False, "", "no keep hook"),
    ],
)
def test_rollback_says_what_was_kept(site, keep, printed, note):
    s, server = site
    server.state["live"] = "blue"
    assert bg.deploy(s) == 0
    if not keep:
        del s.manifests["site"]["hooks"]["keep"]
    said = []
    s.say = said.append
    real = server.ssh

    def ssh(login, command, **kw):
        r = real(login, command, **kw)
        if command.endswith("snapshot-db staging keep"):
            r = subprocess.CompletedProcess([], 0, printed.encode(), b"")
        return r

    server.ssh = ssh
    assert bg.rollback(s) == 0
    assert any(note in line for line in said if line.startswith("Rolling back"))
