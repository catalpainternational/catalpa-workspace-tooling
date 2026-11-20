"""The project-level ``credentials.yaml`` (``docker/credentials.yaml``), and the registry login
``host provision`` / ``host login`` take from it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from catalpa_tooling import project_credentials as pc
from catalpa_tooling.config import load_project_config
from catalpa_tooling.fcos import cli
from catalpa_tooling.fcos import host as h

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"
needs_compose = pytest.mark.skipif(shutil.which("docker") is None, reason="needs docker compose")
TOKEN = "ghp_not-a-real-token"


def plain(path: Path) -> dict:
    """Stands in for sops: the fixtures' credentials files are plain YAML."""
    return yaml.safe_load(path.read_text()) or {}


@pytest.fixture
def site(tmp_path: Path):
    root = tmp_path / "site"
    shutil.copytree(FIXTURES / "site", root)
    return load_project_config(root)


def test_the_env_overrides_the_project(site) -> None:
    project = pc.project_credentials_path(site)
    assert project == site.repo_root / "docker" / "credentials.yaml"
    project.write_text("registry_user: bot\nregistry_token: from-project\nshared: p\nsops: {}\n")
    env = pc.env_credentials_path(site, "staging")
    env.write_text(env.read_text() + "registry_token: from-env\n")
    creds = pc.credentials_for_env(site, "staging", decrypt=plain)
    assert (creds["registry_token"], creds["shared"], creds["registry_user"]) == ("from-env", "p", "bot")
    assert "sops" not in creds


def test_no_project_file_is_just_the_env(site) -> None:
    env = plain(pc.env_credentials_path(site, "staging"))
    env.pop("sops", None)
    assert pc.credentials_for_env(site, "staging", decrypt=plain) == env


def test_registry_login_needs_both_keys() -> None:
    assert pc.registry_login({"registry_user": "bot", "registry_token": TOKEN}) == ("bot", TOKEN)
    assert pc.registry_login({"REGISTRY_USER": "bot", "REGISTRY_TOKEN": TOKEN}) == ("bot", TOKEN)
    assert pc.registry_login({"registry_user": "bot"}) is None
    assert pc.registry_login({}) is None


@needs_compose
def test_the_render_sees_the_project_files_names(tmp_path: Path) -> None:
    """A credential moved from the env's file to the project's renders the same."""
    from catalpa_tooling.fcos.render import compare_render, render_env
    from tests.test_fcos_render import _fake_adapter

    root = tmp_path / "bero"
    shutil.copytree(FIXTURES / "bero", root)
    env = root / "docker" / "envs" / "staging" / "credentials.yaml"
    lines = env.read_text().splitlines(keepends=True)
    moved = [line for line in lines if line.startswith("django_secret_key:")]
    env.write_text("".join(line for line in lines if line not in moved))
    (root / "docker" / "credentials.yaml").write_text("".join(moved) + "registry_token: x\n")
    render = render_env(load_project_config(root), "staging", adapter=_fake_adapter())
    assert compare_render(root / "expected", render.files) == []


class LoginHost:
    """Records SSH commands; ``logged_in`` says what ``podman login --get-login`` returns."""

    address = "203.0.113.7"

    def __init__(self, logged_in: bool = False, login_rc: int = 0) -> None:
        self.logged_in = logged_in
        self.login_rc = login_rc
        self.calls: list[tuple[str, bytes | None]] = []
        self.interactive_calls: list[str] = []

    def ssh(self, login, command, *, input=None, check=True):
        self.calls.append((command, input))
        if "--get-login" in command:
            ok = self.logged_in
            return subprocess.CompletedProcess([], 0 if ok else 1, b"bot\n" if ok else b"", b"")
        if "--password-stdin" in command:
            self.logged_in = self.login_rc == 0
            err = f"bad credentials for {input.decode()}".encode()
            return subprocess.CompletedProcess([], self.login_rc, b"", err)
        return subprocess.CompletedProcess([], 0, b"", b"")

    def interactive(self, login, command) -> int:
        self.interactive_calls.append(command)
        return 0


@pytest.fixture
def committed() -> h.Committed:
    return h.load_committed(FIXTURES / "site" / "expected")


CREDS = {"registry_user": "bot", "registry_token": TOKEN}


def test_provision_logs_in_from_sops_with_the_token_on_stdin(committed, capsys) -> None:
    target = LoginHost()
    assert cli._registry_login(committed, target, CREDS, force=False, label="x") == 0
    (cmd, stdin), = [c for c in target.calls if "--password-stdin" in c[0]]
    assert stdin == TOKEN.encode() and TOKEN not in cmd
    assert "--username bot" in cmd and cmd.endswith("ghcr.io >/dev/null")
    assert TOKEN not in capsys.readouterr().err


def test_already_logged_in_is_left_alone_unless_forced(committed) -> None:
    target = LoginHost(logged_in=True)
    assert cli._registry_login(committed, target, CREDS, force=False, label="x") == 0
    assert not [c for c in target.calls if "--password-stdin" in c[0]]
    assert cli._registry_login(committed, target, CREDS, force=True, label="x") == 0
    assert [c for c in target.calls if "--password-stdin" in c[0]]


def test_a_failed_login_never_prints_the_token(committed, capsys) -> None:
    target = LoginHost(login_rc=1)
    assert cli._registry_login(committed, target, CREDS, force=False, label="x") == 1
    err = capsys.readouterr().err
    assert TOKEN not in err and "<token>" in err and "expired" in err


def test_without_stored_login_it_asks_or_explains(committed, monkeypatch, capsys) -> None:
    target = LoginHost()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert cli._registry_login(committed, target, {}, force=False, label="x") == 0
    assert target.interactive_calls and "dk secrets" in capsys.readouterr().err
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli._registry_login(committed, LoginHost(), {}, force=False, label="x") == 1
    err = capsys.readouterr().err
    assert "ssh -t deploy@203.0.113.7" in err and "registry_token" in err


def test_host_login_logs_in_again_and_shows_who(committed, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_credentials", lambda config, env: CREDS)
    target = LoginHost(logged_in=True)
    assert cli._login(None, None, "staging", committed, target, dry_run=False) == 0
    assert [c for c in target.calls if "--password-stdin" in c[0]]
    assert "ghcr.io: bot" in capsys.readouterr().err
    dry = LoginHost()
    assert cli._login(None, None, "staging", committed, dry, dry_run=True) == 0
    assert dry.calls == []


def test_host_login_parses_and_routes(site, monkeypatch) -> None:
    from catalpa_tooling.cli.dk_argv import normalize_dk_env_argv
    from catalpa_tooling.dk_cli import _parse_env_argv
    from catalpa_tooling.dk_parser import build_dk_parser

    seen = []
    monkeypatch.setattr(cli, "handle_fcos_command", lambda ns, c, env: seen.append(ns.fcos_command) or 0)
    ns = _parse_env_argv(build_dk_parser(site), normalize_dk_env_argv(["staging", "host", "login"]))
    assert cli.route(ns, site, "staging", "quadlet") == 0 and seen == ["login"]
    assert cli.route(ns, site, "staging", "compose") == 2


def test_dk_secrets_edits_the_project_file(site, monkeypatch, capsys) -> None:
    from catalpa_tooling.dk_parser import build_dk_parser

    ns = build_dk_parser(site).parse_args(["secrets", "--dry-run"])
    assert ns.dk_command == "secrets"
    assert pc.cmd_project_secrets(site, dry_run=True) == 0
    assert "sops docker/credentials.yaml" in capsys.readouterr().err

    ran = []
    monkeypatch.setattr(
        pc.subprocess, "run", lambda argv, **k: ran.append(argv) or subprocess.CompletedProcess(argv, 128)
    )
    assert pc.cmd_project_secrets(site, dry_run=False) == 128
    assert ran == [["sops", "docker/credentials.yaml"]]
    assert r"path_regex: ^docker/credentials\.yaml$" in capsys.readouterr().err
