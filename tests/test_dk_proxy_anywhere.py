"""`dk proxy` is the machine's, not a project's: it runs where there is no tooling.yaml."""

from __future__ import annotations

import sys

import pytest

from catalpa_tooling import dk_cli


def test_dk_proxy_runs_outside_a_project(monkeypatch, tmp_path) -> None:
    seen: list[str] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["dk", "proxy", "reload"])
    monkeypatch.setattr(dk_cli, "cmd_proxy", lambda ns: seen.append(ns.proxy_command) or 0)

    with pytest.raises(SystemExit) as exc:
        dk_cli._main_impl()

    assert exc.value.code == 0
    assert seen == ["reload"]
