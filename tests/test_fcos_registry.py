"""Registry logins for a rendered host's pulls (``fcos/registry.py``): only an ``auto_update``
env keeps one; every other pull logs in to a temporary auth file and removes it."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.fcos import cli, deploy
from catalpa_tooling.fcos import host as h
from catalpa_tooling.fcos import registry as reg

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"
GHCR = ["ghcr.io"]


@pytest.fixture
def committed(tmp_path: Path) -> h.Committed:
    root = tmp_path / "site"
    shutil.copytree(FIXTURES / "site", root)
    c = h.load_committed(root / "expected")
    assert not c.manifest.get("auto_update") and h.private_registries(c) == GHCR
    return c


def _auto(c: h.Committed) -> h.Committed:
    return h.Committed(manifest={**c.manifest, "auto_update": True}, files=c.files)


# --- where the token comes from -------------------------------------------------------------


def test_public_images_need_no_login() -> None:
    assert reg.pull_login([], {}, env_name="staging") is None


def test_the_environment_wins_and_sops_isnt_read(monkeypatch) -> None:
    monkeypatch.setenv(reg.TOKEN_ENV, "env-tok")
    monkeypatch.setenv("GITHUB_ACTOR", "ci-actor")
    login = reg.pull_login(GHCR, lambda: pytest.fail("read sops"), env_name="staging")
    assert (login.user, login.token, login.source) == ("ci-actor", "env-tok", reg.TOKEN_ENV)
    monkeypatch.delenv("GITHUB_ACTOR")
    with pytest.raises(reg.RegistryLoginError, match=reg.USER_ENV):
        reg.pull_login(GHCR, {}, env_name="staging")


def test_sops_then_gh_then_a_prompt(monkeypatch) -> None:
    sops = {"registry_user": "bot", "registry_token": "sops-tok"}
    assert reg.pull_login(GHCR, sops, env_name="staging").source == "registry_token in sops"
    monkeypatch.setattr(reg, "gh_login", lambda: reg.PullLogin("me", "gh-tok", "gh auth token"))
    assert reg.pull_login(GHCR, {}, env_name="staging").token == "gh-tok"
    # gh is only asked for ghcr.io.
    with pytest.raises(reg.RegistryLoginError, match="needs a token for this pull"):
        reg.pull_login(["registry.example"], {}, env_name="staging", interactive=False)
    monkeypatch.setattr(reg, "gh_login", lambda: None)
    monkeypatch.setattr("builtins.input", lambda prompt: "typed-user")
    monkeypatch.setattr("getpass.getpass", lambda prompt: "typed-tok")
    login = reg.pull_login(GHCR, {}, env_name="staging", interactive=True)
    assert (login.user, login.token) == ("typed-user", "typed-tok")


def _gh_answers(monkeypatch, scopes: str) -> None:
    def gh(*args):
        if args[:2] == ("api", "--include"):
            out = f"HTTP/2.0 200 OK\r\nX-Oauth-Scopes: {scopes}\r\n\r\n" + '{"login": "octo"}'
            return subprocess.CompletedProcess(args, 0, out, "")
        return subprocess.CompletedProcess(args, 0, "gho_tok\n", "")

    monkeypatch.setattr(reg, "_gh", gh)


def test_gh_needs_read_packages_and_warns_about_wider_scopes(monkeypatch, capsys) -> None:
    _gh_answers(monkeypatch, "gist, read:org, repo")
    assert reg.gh_login() is None
    assert "gh auth refresh -s read:packages" in capsys.readouterr().err
    _gh_answers(monkeypatch, "read:packages")
    assert reg.gh_login() == reg.PullLogin("octo", "gho_tok", "gh auth token")
    assert capsys.readouterr().err == ""
    _gh_answers(monkeypatch, "read:packages, repo, workflow")
    assert reg.gh_login().token == "gho_tok"
    assert "also has repo, workflow" in capsys.readouterr().err


# --- the pull: token on stdin, temporary auth file ------------------------------------------

FAKE_PODMAN = r"""#!/bin/sh
case "$1" in
  login)
    while [ $# -gt 0 ]; do [ "$1" = --authfile ] && f="$2"; shift; done
    cat > "$f"; cp "$f" "$SEEN/login" ;;
  pull)
    [ "$2" = "-q" ] && [ "$3" = --authfile ] && [ -s "$4" ] && echo "$4" > "$SEEN/pull-auth" || exit 7
    [ -n "$FAIL" ] && exit 1; : ;;
  image) echo id ;;
esac
"""


def _run_pull_script(c, tmp_path, token: str, fail: bool = False):
    bin_dir, seen, run_dir = tmp_path / "bin", tmp_path / "seen", tmp_path / "run"
    for d in (bin_dir, seen, run_dir):
        d.mkdir(exist_ok=True)
    (bin_dir / "podman").write_text(FAKE_PODMAN)
    (bin_dir / "podman").chmod(0o755)
    script = deploy.pull_script(c, ["ghcr.io/example/site-django:v1.2.3"], login_user="ci bot")
    assert token not in script
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "SEEN": str(seen), "XDG_RUNTIME_DIR": str(run_dir)}
    if fail:
        env["FAIL"] = "1"
    r = subprocess.run(["sh", "-c", script], input=token + "\n", text=True, capture_output=True, env=env, check=False)
    return r, seen, run_dir


def test_the_pull_logs_in_from_stdin_and_removes_the_auth_file(committed, tmp_path) -> None:
    r, seen, run_dir = _run_pull_script(committed, tmp_path, "s3cret")
    assert r.returncode == 0, r.stderr
    assert (seen / "login").read_text() == "s3cret"  # podman login --password-stdin got it
    assert (seen / "pull-auth").read_text().strip() == str(run_dir / "dk-pull-auth.json")
    assert list(run_dir.iterdir()) == []  # removed on exit


def test_a_failed_pull_still_removes_the_auth_file(committed, tmp_path) -> None:
    r, _seen, run_dir = _run_pull_script(committed, tmp_path, "s3cret", fail=True)
    assert r.returncode != 0
    assert list(run_dir.iterdir()) == []


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes | None]] = []

    def __call__(self, argv, *, input=None, tty=False):
        self.calls.append((argv[-1], input))
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def test_without_auto_update_the_token_goes_on_stdin_not_in_the_command(committed, monkeypatch) -> None:
    monkeypatch.setenv(reg.TOKEN_ENV, "s3cret")
    monkeypatch.setenv(reg.USER_ENV, "ci-bot")
    r = Recorder()
    deploy.pull(h.Host("x", runner=r), committed, ["ghcr.io/example/site-django:v1.2.3"], {}, env_name="staging")
    (cmd, stdin), = r.calls
    assert cmd.startswith("sh -c ") and "s3cret" not in cmd and "dk-pull-auth.json" in cmd
    assert stdin == b"s3cret\n"


def test_auto_update_pulls_with_the_stored_login(committed, monkeypatch) -> None:
    monkeypatch.setattr(reg, "pull_login", lambda *a, **k: pytest.fail("asked for a token"))
    r = Recorder()
    deploy.pull(h.Host("x", runner=r), _auto(committed), ["ghcr.io/example/site-django:v1.2.3"], {}, env_name="staging")
    (cmd, stdin), = r.calls
    assert cmd == "sh -s" and h.auth_file(committed).encode() in stdin


# --- provision and host login ---------------------------------------------------------------


def test_provision_pulls_every_image_and_drops_a_stored_login(committed, monkeypatch, capsys) -> None:
    pulled = []
    monkeypatch.setattr(deploy, "pull", lambda target, c, images, creds, **k: pulled.append(images) or [])
    monkeypatch.setattr(cli, "_registry_login", lambda *a, **k: pytest.fail("stored a login"))

    class Host(Recorder):
        def __call__(self, argv, *, input=None, tty=False):
            self.calls.append((argv[-1], input))
            out = b"removed\n" if "rm -f" in argv[-1] else b""
            return subprocess.CompletedProcess(argv, 0, out, b"")

    target = h.Host("x", runner=Host())
    assert cli._pull_images(committed, target, {}, env_name="staging", label="t") == 0
    assert pulled == [sorted(deploy.account_images(committed))]
    assert "removed the stored registry login" in capsys.readouterr().err


def test_provision_of_an_auto_update_env_keeps_its_login(committed, monkeypatch) -> None:
    logins = []
    monkeypatch.setattr(deploy, "pull", lambda *a, **k: [])
    monkeypatch.setattr(cli, "_registry_login", lambda c, target, creds, **k: logins.append(k) or 0)
    assert cli._pull_images(_auto(committed), h.Host("x", runner=Recorder()), {}, env_name="staging", label="t") == 0
    assert logins and logins[0]["force"] is False


def test_a_missing_token_stops_provision(committed, capsys) -> None:
    rc = cli._pull_images(committed, h.Host("x", runner=Recorder()), {}, env_name="staging", label="t")
    assert rc == 1 and "needs a token for this pull" in capsys.readouterr().err


def test_host_login_is_for_auto_update_envs(committed, tmp_path, capsys) -> None:
    config = load_project_config(FIXTURES / "site")
    ns = argparse.Namespace()
    r = Recorder()
    assert cli._login(ns, config, "staging", committed, h.Host("x", runner=r), dry_run=False) == 0
    assert r.calls == [] and "keeps no registry login" in capsys.readouterr().err
