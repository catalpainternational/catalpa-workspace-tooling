"""The blue/green test app (tests/fixtures/fcos/bluegreen-app): its contract and its hooks.

The hooks are what dk runs in the site account during a deploy, so they are checked here
against a fake `podman` (a database server kept as a JSON file) and a fake `curl`, with no VM.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent / "fixtures" / "fcos" / "bluegreen-app"
VERSIONS = ("v1", "v2", "v2-broken")

# A database server in a JSON file: the names of its databases. It answers the SQL the
# hooks send through `podman exec ... psql`, and `pg_dump`/`pg_restore`.
FAKE_PODMAN = r"""#!/usr/bin/env python3
import json, os, re, sys
state = os.environ["FAKE_DB"]
dbs = json.load(open(state))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join(args) + "\n")
def save():
    json.dump(dbs, open(state, "w"))
if args[:1] == ["exec"] and "caddy" in args:
    sys.exit(int(os.environ.get("FAKE_RELOAD_EXIT", "0")))
if args[:1] == ["exec"] and "pg_dump" in args:
    name = args[-1]
    if name not in dbs:
        sys.exit(1)
    sys.stdout.write("dump of " + name)
    sys.exit(0)
if args[:1] == ["exec"] and "pg_restore" in args:
    sys.stdin.read()
    sys.exit(0)
if args[:1] == ["exec"] and "psql" in args:
    for stmt in filter(None, (s.strip() for s in sys.stdin.read().split(";"))):
        if m := re.match(r"SELECT 1 FROM pg_database WHERE datname = '(\w+)'", stmt):
            print("1" if m[1] in dbs else "")
        elif stmt.startswith("SELECT pg_terminate_backend"):
            pass
        elif m := re.match(r"ALTER DATABASE (\w+) RENAME TO (\w+)", stmt):
            if m[1] not in dbs or m[2] in dbs:
                sys.exit(1)
            dbs[dbs.index(m[1])] = m[2]
        elif m := re.match(r"CREATE DATABASE (\w+)", stmt):
            if m[1] in dbs:
                sys.exit(1)
            dbs.append(m[1])
        elif m := re.match(r"DROP DATABASE (\w+)", stmt):
            if m[1] not in dbs:
                sys.exit(1)
            dbs.remove(m[1])
        else:
            sys.exit("fake podman: unknown SQL: " + stmt)
    save()
    sys.exit(0)
sys.exit("fake podman: unexpected " + " ".join(args))
"""

FAKE_CURL = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
exit "${FAKE_CURL_EXIT:-0}"
"""


def contract(version: str) -> dict:
    return tomllib.loads((APP / version / "app.toml").read_text())


@pytest.mark.parametrize("version", VERSIONS)
def test_every_unit_the_contract_names_ships_in_the_units_folder(version):
    toml = contract(version)
    units = {p.name for p in (APP / version / "units").iterdir()}
    named = set(toml["site"]["units"]) | set(toml["colour"]["units"])
    assert named == units
    assert not set(toml["site"]["units"]) & set(toml["colour"]["units"])
    for spec in toml["ports"].values():
        side, unit, port = spec.split(":")
        assert unit in toml[side]["units"], spec
        assert int(port) > 0


def test_the_two_units_versions_ship_the_same_names_with_different_content():
    v1, v2 = APP / "v1" / "units", APP / "v2" / "units"
    assert {p.name for p in v1.iterdir()} == {p.name for p in v2.iterdir()}
    assert (v1 / "notes-web@.container").read_text() != (
        v2 / "notes-web@.container"
    ).read_text()
    assert (APP / "v1" / "units-version").read_text().strip() == "1"
    assert (APP / "v2" / "units-version").read_text().strip() == "2"


def test_only_the_broken_version_breaks_its_migration():
    for version in VERSIONS:
        broken = (
            "BREAK_MIGRATION=1"
            in (APP / version / "units" / "notes-migrate@.container").read_text()
        )
        assert broken == (version == "v2-broken"), version


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A site account's home: its hooks, a fake podman and curl on PATH, one database."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    for name, body in (("podman", FAKE_PODMAN), ("curl", FAKE_CURL)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "dbs.json"
    state.write_text(json.dumps(["postgres", "app"]))
    log = tmp_path / "calls.log"
    log.touch()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FAKE_DB", str(state))
    monkeypatch.setenv("FAKE_LOG", str(log))

    class Site:
        def run(self, hook: str, *args: str, **env: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [str(APP / "v1" / "bin" / hook), *args],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, **env},
            )

        def databases(self) -> list[str]:
            return json.loads(state.read_text())

        state_dir = home / ".local" / "state" / "notes" / "staging"
        calls = log

    return Site()


def test_freeze_sets_the_database_aside_and_thaw_puts_it_back(site):
    assert site.run("snapshot-db", "staging", "frozen").returncode == 1
    assert site.run("snapshot-db", "staging", "freeze").returncode == 0
    assert sorted(site.databases()) == ["app", "app_before", "postgres"]
    assert site.run("snapshot-db", "staging", "frozen").returncode == 0
    assert site.run("snapshot-db", "staging", "thaw").returncode == 0
    assert sorted(site.databases()) == ["app", "postgres"]
    assert site.run("snapshot-db", "staging", "frozen").returncode == 1


def test_a_second_freeze_is_refused_so_the_first_set_aside_is_never_overwritten(site):
    assert site.run("snapshot-db", "staging", "freeze").returncode == 0
    second = site.run("snapshot-db", "staging", "freeze")
    assert second.returncode == 2
    assert "already set aside" in second.stderr
    assert sorted(site.databases()) == ["app", "app_before", "postgres"]


def test_thaw_with_nothing_set_aside_is_refused_and_keeps_the_database(site):
    thaw = site.run("snapshot-db", "staging", "thaw")
    assert thaw.returncode == 2
    assert sorted(site.databases()) == ["app", "postgres"]


def test_take_frozen_dumps_the_set_aside_database_and_drops_it(site):
    site.run("snapshot-db", "staging", "freeze")
    taken = site.run("snapshot-db", "staging", "take-frozen")
    assert taken.returncode == 0, taken.stderr
    dump = Path(taken.stdout.strip())
    assert dump.parent == site.state_dir / "snapshots"
    assert dump.read_text() == "dump of app_before"
    assert not list(dump.parent.glob("*.part"))
    assert sorted(site.databases()) == ["app", "postgres"]


def test_restore_replaces_the_database_from_a_dump(site):
    site.run("snapshot-db", "staging", "freeze")
    dump = site.run("snapshot-db", "staging", "take-frozen").stdout.strip()
    restored = site.run("snapshot-db", "staging", "restore", dump)
    assert restored.returncode == 0, restored.stderr
    assert sorted(site.databases()) == ["app", "postgres"]
    assert "pg_restore" in site.calls.read_text()


def test_keep_dumps_the_database_into_kept_and_leaves_it_in_place(site):
    kept = site.run("snapshot-db", "staging", "keep")
    assert kept.returncode == 0, kept.stderr
    dump = Path(kept.stdout.strip())
    assert dump.parent == site.state_dir / "snapshots" / "kept"
    assert dump.read_text() == "dump of app"
    assert not list(dump.parent.glob("*.part"))
    assert sorted(site.databases()) == ["app", "postgres"]


def test_keep_with_no_database_prints_nothing_and_succeeds(site):
    state = Path(os.environ["FAKE_DB"])
    state.write_text(json.dumps([d for d in site.databases() if d != "app"]))
    kept = site.run("snapshot-db", "staging", "keep")
    assert kept.returncode == 0, kept.stderr
    assert kept.stdout == ""
    assert not (site.state_dir / "snapshots" / "kept").exists()


def test_restore_without_a_dump_is_refused_before_dropping_anything(site, tmp_path):
    restored = site.run(
        "snapshot-db", "staging", "restore", str(tmp_path / "missing.dump")
    )
    assert restored.returncode == 2
    assert sorted(site.databases()) == ["app", "postgres"]


def test_switch_points_the_front_at_the_colour_port_and_records_it(site):
    switched = site.run("switch-site", "staging", "green", "8212")
    assert switched.returncode == 0, switched.stderr
    caddyfile = (site.state_dir / "front" / "Caddyfile").read_text()
    assert "reverse_proxy 169.254.1.3:8212" in caddyfile
    assert (site.state_dir / "live").read_text().strip() == "green"
    assert "http://127.0.0.1:8212/healthz" in site.calls.read_text()


def test_switch_to_a_colour_that_does_not_answer_leaves_the_front_alone(site):
    site.run("switch-site", "staging", "blue", "8211")
    refused = site.run("switch-site", "staging", "green", "8212", FAKE_CURL_EXIT="7")
    assert refused.returncode == 1
    assert (
        "reverse_proxy 169.254.1.3:8211"
        in (site.state_dir / "front" / "Caddyfile").read_text()
    )
    assert (site.state_dir / "live").read_text().strip() == "blue"


def test_switch_the_front_refuses_puts_the_old_config_back(site):
    site.run("switch-site", "staging", "blue", "8211")
    refused = site.run("switch-site", "staging", "green", "8212", FAKE_RELOAD_EXIT="1")
    assert refused.returncode == 1
    assert (
        "reverse_proxy 169.254.1.3:8211"
        in (site.state_dir / "front" / "Caddyfile").read_text()
    )
    assert (site.state_dir / "live").read_text().strip() == "blue"


@pytest.mark.parametrize(
    "args", [("staging", "red", "8211"), ("staging", "blue", "80;rm")]
)
def test_switch_refuses_a_bad_colour_or_port(site, args):
    assert site.run("switch-site", *args).returncode == 2
    assert not (site.state_dir / "front" / "Caddyfile").exists()
