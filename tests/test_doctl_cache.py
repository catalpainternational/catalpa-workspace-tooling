"""doctl lookups are cached within one dk command; any change empties the cache."""

from __future__ import annotations

import subprocess

import pytest

from catalpa_tooling import doctl_binary as db


@pytest.fixture
def doctl(monkeypatch):
    calls: list[list[str]] = []
    state = {"droplets": '[{"id": 1}]'}

    def fake_run_cmd(cmd, **kwargs):
        calls.append(list(cmd))
        out = state["droplets"] if "list" in cmd else "{}"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(db, "ensure_doctl_available", lambda: "doctl")
    monkeypatch.setattr(db, "run_cmd", fake_run_cmd)
    return calls, state


def test_a_repeated_lookup_runs_doctl_once_and_returns_a_copy(doctl) -> None:
    calls, _ = doctl
    first = db.run_doctl_json(["compute", "droplet", "list"])
    first.append("mutated by a caller")
    assert db.run_doctl_json(["compute", "droplet", "list"]) == [{"id": 1}]
    assert len(calls) == 1
    db.run_doctl_json(["compute", "droplet", "list"], context="other")  # another account
    assert len(calls) == 2


def test_a_change_empties_the_cache(doctl) -> None:
    calls, state = doctl
    db.run_doctl_json(["compute", "droplet", "list"])
    state["droplets"] = '[{"id": 1}, {"id": 2}]'
    db.run_doctl(["compute", "droplet", "create", "x", "--wait"])
    assert db.run_doctl_json(["compute", "droplet", "list"]) == [{"id": 1}, {"id": 2}]
    assert len(calls) == 3


@pytest.mark.parametrize(
    "args,read",
    [(["compute", "domain", "records", "list", "catalpa.io"], True), (["compute", "droplet", "get", "1"], True),
     (["compute", "domain", "records", "create", "x", "--record-name", "list"], False),
     (["projects", "resources", "assign", "p"], False)],
)  # fmt: skip
def test_what_counts_as_a_read(args, read) -> None:
    assert db._is_read(args) is read


def test_the_project_poll_asks_again_each_try(monkeypatch) -> None:
    from catalpa_tooling import doctl_projects as dp

    tries = []
    cleared = []
    monkeypatch.setattr(db, "clear_doctl_cache", lambda: cleared.append(1))
    monkeypatch.setattr(dp, "find_project_droplet_by_name", lambda *a, **k: tries.append(1) or (None if len(tries) < 3 else {"id": 9}))
    monkeypatch.setattr(dp.time, "sleep", lambda s: None)
    assert dp.wait_for_project_droplet_by_name("p", "x", context=None) == {"id": 9}
    assert len(cleared) == len(tries) == 3
