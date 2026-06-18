"""Routing tests for ``dk digoc``."""

from __future__ import annotations

import sys

import pytest

from catalpa_tooling import dk_cli
from catalpa_tooling import doctl_cli
from tests.helpers import write_minimal_tooling_tree


def test_run_digoc_help() -> None:
    assert doctl_cli.run_digoc(["--help"]) == 0


def test_run_digoc_unknown_command() -> None:
    assert doctl_cli.run_digoc(["not-a-command"]) == 2


def test_dk_digoc_dispatches(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    write_minimal_tooling_tree(tmp_path)
    monkeypatch.chdir(tmp_path)

    calls: list[list[str]] = []

    def fake_dispatch_digoc(ns) -> int:
        calls.append([getattr(ns, "digoc_command", ""), getattr(ns, "droplets_command", "")])
        return 0

    monkeypatch.setattr("catalpa_tooling.dk_cli.dispatch_digoc", fake_dispatch_digoc)
    monkeypatch.setattr(sys, "argv", ["dk", "digoc", "droplets", "list"])

    with pytest.raises(SystemExit) as exc:
        dk_cli.main()
    assert exc.value.code == 0
    assert calls == [["droplets", "list"]]

def test_digoc_not_treated_as_deploy_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """``digoc`` is a top-level dk command, not ``docker/envs/digoc``."""
    write_minimal_tooling_tree(tmp_path)
    deploy = tmp_path / "docker" / "envs" / "digoc"
    deploy.mkdir(parents=True)
    (deploy / "info.yaml").write_text("description: would conflict\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(
        "catalpa_tooling.doctl_cli.run_digoc",
        lambda argv: 0,
    )
    monkeypatch.setattr(sys, "argv", ["dk", "digoc", "--help"])

    with pytest.raises(SystemExit) as exc:
        dk_cli.main()
    assert exc.value.code == 0
