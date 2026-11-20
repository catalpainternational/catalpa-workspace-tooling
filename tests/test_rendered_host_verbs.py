"""Tooling 2.0 M2: ``dk <env> render|check|diff|apply``, ``host ignition|provision`` and
``secrets push`` as plain env verbs, and ``dk <env> fcos …`` as their deprecated alias."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalpa_tooling.cli.dk_argv import is_implicit_compose_argv, normalize_dk_env_argv
from catalpa_tooling.config import load_project_config
from catalpa_tooling.dk_parser import build_dk_parser
from catalpa_tooling.fcos import cli
from tests.helpers import write_minimal_tooling_tree


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    write_minimal_tooling_tree(tmp_path)
    for env, extra in (("staging", "runtime: quadlet\n"), ("prod", "")):
        d = tmp_path / "docker" / "envs" / env
        d.mkdir(parents=True)
        (d / "info.yaml").write_text(f"name: {env}\n{extra}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return load_project_config(tmp_path)


def parse(config, *argv: str):
    return build_dk_parser(config).parse_args(normalize_dk_env_argv(list(argv)))


def test_the_verbs_parse(config) -> None:
    ns = parse(config, "staging", "render")
    assert ns.env_command == "render"
    ns = parse(config, "staging", "diff", "--names-only", "--host", "203.0.113.9")
    assert (ns.env_command, ns.names_only, ns.host) == ("diff", True, "203.0.113.9")
    ns = parse(config, "staging", "apply", "--no-restart", "--force")
    assert (ns.env_command, ns.no_restart, ns.force) == ("apply", True, True)
    ns = parse(config, "staging", "host", "provision", "--no-start", "--host", "203.0.113.9")
    assert (ns.env_command, ns.host_command, ns.no_start, ns.host) == ("host", "provision", True, "203.0.113.9")
    ns = parse(config, "staging", "host", "ignition", "-o", "x.ign")
    assert (ns.host_command, ns.output) == ("ignition", "x.ign")
    ns = parse(config, "staging", "secrets", "push", "a", "b", "--restart")
    assert (ns.env_command, ns.secrets_command, ns.names, ns.restart) == ("secrets", "push", ["a", "b"], True)
    # A bare `secrets` still edits credentials.yaml.
    ns = parse(config, "staging", "secrets")
    assert ns.env_command == "secrets" and getattr(ns, "secrets_command", None) is None


@pytest.mark.parametrize("where", ["before", "after"])
def test_dry_run_and_yes_work_before_or_after_the_verb(config, where) -> None:
    """The verbs' own flags default to SUPPRESS, so they don't reset the env-level ones."""
    argv = ["staging", "--dry-run", "-y", "apply"] if where == "before" else ["staging", "apply", "--dry-run", "-y"]
    ns = parse(config, *argv)
    assert ns.dry_run is True and ns.yes is True
    ns = parse(config, "staging", "apply")
    assert ns.dry_run is False and ns.yes is False


def test_verbs_are_not_compose_passthrough() -> None:
    for verb in cli.RENDER_VERBS:
        assert is_implicit_compose_argv(["staging", verb]) is False


@pytest.mark.parametrize(
    "argv,fcos_command",
    [
        (["render"], "render"),
        (["check"], "check"),
        (["diff"], "diff"),
        (["apply"], "apply"),
        (["host", "ignition", "-o", "x"], "ignition"),
        (["host", "provision"], "provision"),
        (["secrets", "push"], "secrets"),
    ],
)
def test_each_verb_runs_the_rendered_host_command(config, monkeypatch, argv, fcos_command) -> None:
    seen = []
    monkeypatch.setattr(cli, "handle_fcos_command", lambda ns, c, env: seen.append(ns) or 0)
    ns = parse(config, "staging", *argv)
    assert cli.route(ns, config, "staging", "quadlet") == 0
    assert seen[0].fcos_command == fcos_command
    if fcos_command == "secrets":
        assert seen[0].fcos_secrets_command == "push"


def test_other_commands_are_not_routed(config) -> None:
    for argv in (["info"], ["secrets"], ["host"], ["host", "logging"]):
        assert cli.route(parse(config, "staging", *argv), config, "staging", "quadlet") is None


def test_a_compose_env_is_told_these_are_for_rendered_hosts(config, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "handle_fcos_command", lambda *a: pytest.fail("ran"))
    assert cli.route(parse(config, "prod", "apply"), config, "prod", "compose") == 2
    assert "runtime: quadlet" in capsys.readouterr().err


def test_fcos_spelling_still_works_with_a_note(config, monkeypatch, capsys) -> None:
    seen = []
    monkeypatch.setattr(cli, "handle_fcos_command", lambda ns, c, env: seen.append(ns.fcos_command) or 0)
    for argv, new in (
        (["fcos", "render"], "dk staging render"),
        (["fcos", "provision"], "dk staging host provision"),
        (["fcos", "secrets", "push"], "dk staging secrets push"),
    ):
        assert cli.route(parse(config, "staging", *argv), config, "staging", "quadlet") == 0
        assert f"is now `{new}`" in capsys.readouterr().err
    assert seen == ["render", "provision", "secrets"]


def test_through_dk(config, monkeypatch) -> None:
    """The env handler sends the verbs to the rendered-host command, before the quadlet backend."""
    from catalpa_tooling.env_handlers import handle_env_command

    seen = []
    monkeypatch.setattr(cli, "handle_fcos_command", lambda ns, c, env: seen.append(ns.fcos_command) or 0)
    assert handle_env_command(parse(config, "staging", "diff"), config) == 0
    assert handle_env_command(parse(config, "staging", "host", "provision"), config) == 0
    assert seen == ["diff", "provision"]
