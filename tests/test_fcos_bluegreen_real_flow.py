"""Where dk's blue/green deploy differs from Partisipa's real one (containerwharf's site
deploy in containerwharf/sites.py, and deploy-site/rollback-site in partisipa-import's
infra/quadlets/bin). Each test states what the real deploy does; dk should do the same."""

from __future__ import annotations

from catalpa_tooling.fcos import bluegreen as bg
from tests.test_fcos_bluegreen import _steps, site  # noqa: F401 - the fixture


def test_a_reboot_during_the_outage_does_not_start_the_old_colour(site):  # noqa: F811
    """Real: the live colour leaves the boot before it stops ("a reboot from here must not
    start it on the copy being migrated"), and comes back only once it is live again. So a
    deploy whose put-back failed leaves no colour starting at boot."""
    s, server = site
    server.state["live"] = "blue"
    server.links["blue"] = {"wanted"}
    server.fails_to_start.add("green")
    server.hook_rc["thaw"] = 1
    assert bg.deploy(s) == 1
    assert server.links["blue"] == set()
    assert server.links["green"] == set()


def test_a_put_back_that_works_puts_the_old_colour_back_in_the_boot(site):  # noqa: F811
    s, server = site
    server.state["live"] = "blue"
    server.links["blue"] = {"wanted"}
    server.fails_to_start.add("green")
    assert bg.deploy(s) == 1
    assert server.links["blue"] == {"wanted"}
    assert server.links["green"] == set()


def test_rollback_recovers_from_a_failed_put_back(site):  # noqa: F811
    """Real: `sites rollback` thaws what is still set aside, or, with nothing set aside,
    goes back to the old copy alone. dk's rollback needs a snapshot, which a deploy that
    failed mid-way never took, so the mark would stay for good."""
    s, server = site
    server.state["live"] = "blue"
    server.fails_to_start.add("green")
    server.hook_rc["thaw"] = 1
    assert bg.deploy(s) == 1
    assert "mark" in server.state
    server.fails_to_start.clear()
    server.hook_rc["thaw"] = 0
    server.hook_rc["frozen"] = 0  # the database is still set aside
    server.calls.clear()
    assert bg.rollback(s) == 0
    assert ("site", "hook thaw staging") in _steps(server)
    assert server.state["live"] == "blue"
    assert "mark" not in server.state


def test_a_deploy_that_changes_the_idle_colour_drops_the_old_rollback_record(site):  # noqa: F811
    """Real: once the idle copy holds the new release, the rollback record goes (deploy-site:
    "the idle copy now holds the new ref, so it is no longer something to go back to").
    Otherwise `rollback` starts the idle colour's NEW units against the OLD snapshot."""
    s, server = site
    server.state["live"] = "blue"
    assert bg.deploy(s) == 0  # green live; the record says "go back to blue"
    assert server.state["rollback"].startswith("blue ")
    server.hook_rc["frozen"] = 2  # the next deploy, to blue, stops before the freeze
    assert bg.deploy(s) == 4
    assert "rollback" not in server.state


def test_a_failed_freeze_is_thawed_by_the_put_back(site):  # noqa: F811
    """Real: containerwharf sets `froze=1` before running freeze, so a freeze that failed
    part-way (renamed, then could not copy) is thawed by the put-back."""
    s, server = site
    server.state["live"] = "blue"
    server.hook_rc["freeze"] = 1
    assert bg.deploy(s) == 1
    assert ("site", "hook thaw staging") in _steps(server)
