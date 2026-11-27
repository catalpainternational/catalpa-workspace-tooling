"""Tooling 2.0 M3: no implicit compose passthrough on a compose env.

``dk <env> up -d``, ``dk <env> config`` … are refused with a hint (``dk <env> compose …``); a
bare ``dk <env>`` is still ``up -d``, and the running app's verbs (``ps``, ``logs``, ``exec``,
``start``/``stop``/``restart``) stay, as on every runtime."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from catalpa_tooling.cli.dk_argv import (
    COMPOSE_VERBS,
    RUNNING_APP_VERBS,
    normalize_dk_env_argv,
)
from catalpa_tooling.dk_cli import _parse_dk
from catalpa_tooling.dk_parser import build_dk_parser
from catalpa_tooling.env_handlers import handle_env_command


@pytest.fixture
def run(minimal_project):
    """``dk dev <argv…>`` through the real parser; returns (rc, the compose args run or None)."""
    env_dir = minimal_project.deploy_envs_dir / "dev"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "info.yaml").write_text("name: dev\ncredentials_decrypt_optional: true\n")
    (env_dir / "credentials.yaml").write_text("{}\n")

    def go(*argv: str):
        seen = []
        with patch(
            "catalpa_tooling.env_handlers._run_compose_path",
            side_effect=lambda ns, config, **k: seen.append(k["compose_args"]) or 0,
        ):
            ns = _parse_dk(minimal_project, ["dev", *argv], build_dk_parser(minimal_project))
            rc = handle_env_command(ns, minimal_project)
        return rc, (seen[0] if seen else None)

    return go


def test_bare_dk_env_is_still_up_d(run) -> None:
    assert run() == (0, ["up", "-d"])


@pytest.mark.parametrize("argv", [("logs", "-f", "django"), ("ps",), ("exec", "django", "bash"),
                                  ("restart", "django"), ("stop",), ("start", "db")])  # fmt: skip
def test_the_running_apps_verbs_stay(run, argv) -> None:
    assert run(*argv) == (0, list(argv))


@pytest.mark.parametrize("verb", sorted(COMPOSE_VERBS - RUNNING_APP_VERBS))
def test_every_other_compose_verb_is_refused_with_a_hint(run, capsys, verb) -> None:
    assert run(verb, "-x") == (2, None)
    err = capsys.readouterr().err
    assert f"`dk dev compose {verb} -x`" in err


def test_up_points_at_the_bare_command(run, capsys) -> None:
    assert run("up", "-d") == (2, None)
    err = capsys.readouterr().err
    assert "A bare `dk dev`" in err and "`dk dev compose up -d`" in err


def test_explicit_compose_still_runs_anything(run) -> None:
    assert run("compose", "up", "-d") == (0, ["up", "-d"])
    assert run("compose", "down", "-v") == (0, ["down", "-v"])


def test_a_worktree_still_brings_its_stack_up_and_down(monkeypatch, minimal_project) -> None:
    """``dk worktree up``/``down`` drive the base env's compose with the explicit form."""
    from catalpa_tooling import worktree

    seen = []
    monkeypatch.setattr(
        worktree, "_resolve_worktree", lambda config, slug: (None, type("O", (), {"base_env": "dev"}), config)
    )
    monkeypatch.setattr("catalpa_tooling.env_handlers.handle_env_command", lambda ns, c: seen.append(ns) or 0)
    for args in (["up", "-d"], ["down"]):
        assert worktree._run_worktree_env_compose(minimal_project, slug="x", compose_args=args, dry_run=True) == 0
    assert [(ns.env_command, ns.compose_argv) for ns in seen] == [("compose", ["up", "-d"]), ("compose", ["down"])]


def test_normalised_argv_unchanged() -> None:
    """Parsing is unchanged: the refusal is in the handler, where the runtime is known (a
    quadlet env refuses these verbs with its own hints)."""
    assert normalize_dk_env_argv(["dev", "up", "-d"]) == ["dev", "up", "-d"]


# --- M3b: `db backup` defaults to incr -----------------------------------------------------


@pytest.mark.parametrize(("argv", "expected"), [((), "incr"), (("full",), "full"), (("diff",), "diff")])
def test_db_backup_defaults_to_incr(minimal_project, argv, expected) -> None:
    env_dir = minimal_project.deploy_envs_dir / "dev"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "info.yaml").write_text("name: dev\n")
    parser = build_dk_parser(minimal_project)
    for verb in ("db", "bkp_db"):
        ns = _parse_dk(minimal_project, ["dev", verb, "backup", *argv], parser)
        assert ns.backup_type == expected


def test_db_backup_still_rejects_an_unknown_type(minimal_project) -> None:
    env_dir = minimal_project.deploy_envs_dir / "dev"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "info.yaml").write_text("name: dev\n")
    with pytest.raises(SystemExit):
        _parse_dk(minimal_project, ["dev", "db", "backup", "weekly"], build_dk_parser(minimal_project))
