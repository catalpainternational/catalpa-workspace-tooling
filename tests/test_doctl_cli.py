"""Routing tests for doctl CLI."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from catalpa_tooling import doctl_cli


def test_auth_init_adds_interactive_on_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    monkeypatch.setattr(doctl_cli.sys.stdin, "isatty", lambda: True)
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(doctl_cli, "run_doctl", mock_run)
    assert doctl_cli._cmd_auth_init([]) == 0
    mock_run.assert_called_once_with(["auth", "init", "--interactive"])


def test_auth_init_passes_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    monkeypatch.setattr(doctl_cli.sys.stdin, "isatty", lambda: False)
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(doctl_cli, "run_doctl", mock_run)
    assert doctl_cli._cmd_auth_init(["-t", "secret"]) == 0
    mock_run.assert_called_once_with(["auth", "init", "--access-token", "secret"])


def test_auth_init_non_tty_without_token_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    monkeypatch.setattr(doctl_cli.sys.stdin, "isatty", lambda: False)
    assert doctl_cli._cmd_auth_init([]) == 1


def test_auth_remove_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(doctl_cli, "run_doctl", mock_run)
    assert doctl_cli._cmd_auth_forward("remove", ["--context", "default"]) == 0
    mock_run.assert_called_once_with(["auth", "remove", "--context", "default"])


def test_auth_list_invokes_doctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(doctl_cli, "run_doctl", mock_run)
    assert doctl_cli._cmd_auth_list([]) == 0
    mock_run.assert_called_once_with(["auth", "list"], context=None)


def test_projects_list_invokes_doctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(doctl_cli, "run_doctl", mock_run)
    assert doctl_cli._cmd_projects_list([]) == 0
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0:2] == ["projects", "list"]


def test_droplets_list_with_project_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    monkeypatch.setattr(
        doctl_cli,
        "resolve_project_id",
        lambda project, do_config=None, context=None: "proj-1",
    )
    listed: list[str] = []

    def fake_list(project_id: str, *, context, columns, as_json: bool) -> int:
        listed.append(project_id)
        return 0

    monkeypatch.setattr(doctl_cli, "list_project_droplets", fake_list)
    monkeypatch.setattr(
        doctl_cli.ProjectConfig,
        "from_cwd",
        staticmethod(
            lambda: (_ for _ in ()).throw(doctl_cli.ProjectConfigError("no manifest"))
        ),
    )
    assert doctl_cli._cmd_droplets_list(["--project", "my-proj"]) == 0
    assert listed == ["proj-1"]
