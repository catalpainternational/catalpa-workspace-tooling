"""A bare ``dk <env>`` on a ``runtime: quadlet`` env (``fcos/deploy.py``), with SSH faked."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.fcos import backend
from catalpa_tooling.fcos import deploy as d
from catalpa_tooling.fcos import host as h
from tests.test_fcos_host import in_sync_state

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"
DJANGO = "ghcr.io/example/site-django:v1.2.3"
INFO = {"docker_host": "ssh://deploy@staging.site.example/run/user/1001/podman/podman.sock"}


@pytest.fixture
def site(tmp_path: Path):
    root = tmp_path / "site"
    shutil.copytree(FIXTURES / "site", root)
    shutil.copytree(root / "expected", root / "docker" / "envs" / "staging" / "generated" / "fcos")
    return load_project_config(root)


@pytest.fixture(autouse=True)
def rendered(monkeypatch):
    """The render and its check have their own tests (and need Docker for Caddy): here the
    committed render is current unless a test says otherwise."""
    from catalpa_tooling.fcos import cli, render

    state = {"check": 0}
    monkeypatch.setattr(render, "render_env", lambda *a, **k: object())
    monkeypatch.setattr(cli, "_check", lambda *a, **k: state["check"])
    return state


@pytest.fixture(autouse=True)
def pull_token(monkeypatch):
    """The site fixture's images are on ghcr.io: each pull gets a token (CI's way)."""
    monkeypatch.setenv("DK_REGISTRY_TOKEN", "tok-123")
    monkeypatch.setenv("DK_REGISTRY_USER", "ci-bot")


@pytest.fixture
def committed(site) -> h.Committed:
    return h.load_committed(site.deploy_envs_dir / "staging" / "generated" / "fcos")


class FakeHost:
    """Answers the pull script with ``changed`` and ``is-active`` with ``states``."""

    def __init__(self, changed: list[str] = (), states: str = "active") -> None:
        self.changed = list(changed)
        self.states = states
        self.calls: list[tuple[str, bytes | None]] = []

    def __call__(self, argv, *, input=None, tty=False):
        cmd = argv[-1]
        self.calls.append((cmd, input))
        out = b""
        pulling = (cmd == "sh -s" and input and b"podman pull" in input) or (
            cmd.startswith("sh -c ") and "podman pull" in cmd
        )
        if pulling:
            out = "".join(f"{d.CHANGED}{i}\n" for i in self.changed).encode()
        elif "is-active" in cmd:
            out = "\n".join(self.states for _ in cmd.split()[3:]).encode()
        return subprocess.CompletedProcess(argv, 0, out, b"")

    @property
    def restarts(self) -> list[str]:
        return [c for c, _ in self.calls if c.startswith("systemctl --user restart")]


def run_deploy(site, fake: FakeHost, **kw) -> int:
    kw = {"yes": True, "dry_run": False, "tag": None, **kw}
    return d.deploy(site, "staging", INFO, target=h.Host("staging.site.example", runner=fake), **kw)


def test_images_and_their_units(committed) -> None:
    images = d.account_images(committed)
    assert images[DJANGO] == ["collectstatic", "django", "migrate"]
    # migrate and collectstatic re-run with django (PartOf=), so django is the one restart.
    assert d.units_for_images(committed, [DJANGO]) == ["django.service"]
    assert d.units_for_images(committed, []) == []


FAKE_PODMAN = r"""#!/bin/sh
# podman image inspect --format '{{.Id}}' IMG | podman pull -q [--authfile F] IMG
for img; do :; done
f="$IDS/$(echo "$img" | tr '/:' '__')"
case "$1" in
  image) [ "$(cat "$PODMAN_FMT")" = "$4" ] || exit 9; [ -f "$f" ] && cat "$f" || exit 125 ;;
  pull) [ -f "$f.next" ] && mv "$f.next" "$f"; [ -f "$f" ] || echo first > "$f" ;;
esac
"""


def test_pull_script_reports_only_changed_images(committed, tmp_path) -> None:
    """The real script, run by sh against a fake podman: an image whose ID moved (or that
    wasn't there before) is reported; one that stayed isn't."""
    bin_dir, ids = tmp_path / "bin", tmp_path / "ids"
    bin_dir.mkdir(), ids.mkdir()
    podman = bin_dir / "podman"
    podman.write_text(FAKE_PODMAN)
    podman.chmod(0o755)
    (tmp_path / "fmt").write_text("{{.Id}}")
    same, moved, new = "docker.io/a:1", "ghcr.io/x/b:2", "ghcr.io/x/c:3"
    (ids / "docker.io_a_1").write_text("aaa")
    (ids / "ghcr.io_x_b_2").write_text("bbb")
    (ids / "ghcr.io_x_b_2.next").write_text("bbb2")
    out = subprocess.run(
        ["sh", "-s"], input=d.pull_script(committed, [same, moved, new]), capture_output=True,
        text=True, env={"PATH": f"{bin_dir}:/usr/bin:/bin", "IDS": str(ids),
                        "PODMAN_FMT": str(tmp_path / "fmt"), "HOME": str(tmp_path)},
    )  # fmt: skip
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [f"{d.CHANGED}{moved}", f"{d.CHANGED}{new}"]


def test_a_new_image_restarts_its_unit(site, committed, monkeypatch, capsys) -> None:
    monkeypatch.setattr(h, "read_host", lambda host, c: in_sync_state(c))
    fake = FakeHost(changed=[DJANGO])
    assert run_deploy(site, fake) == 0
    assert fake.restarts == ["systemctl --user restart django.service"]
    err = capsys.readouterr().err
    assert "files match the render" in err and f"New images: {DJANGO}" in err and "up to date" in err


def test_nothing_new_restarts_nothing(site, monkeypatch, capsys) -> None:
    monkeypatch.setattr(h, "read_host", lambda host, c: in_sync_state(c))
    fake = FakeHost()
    assert run_deploy(site, fake) == 0
    assert fake.restarts == []
    assert "every image is current" in capsys.readouterr().err


def test_changed_files_are_applied_and_not_restarted_twice(site, monkeypatch) -> None:
    from catalpa_tooling.fcos import cli

    def stale(host, c):
        s = in_sync_state(c)
        s.files[c.manifest["install"][0]["dest"]] = None
        return s

    applied = []
    monkeypatch.setattr(h, "read_host", stale)
    monkeypatch.setattr(cli, "apply_plan", lambda *a, **k: applied.append(k) or (0, ["django.service"]))
    fake = FakeHost(changed=[DJANGO])
    assert run_deploy(site, fake) == 0
    assert applied and applied[0]["yes"] is True
    assert fake.restarts == []  # apply already restarted django


def test_missing_secrets_stop_before_anything_changes(site, monkeypatch, capsys) -> None:
    def no_secrets(host, c):
        s = in_sync_state(c)
        s.secrets = set()
        return s

    monkeypatch.setattr(h, "read_host", no_secrets)
    fake = FakeHost(changed=[DJANGO])
    assert run_deploy(site, fake) == 1
    assert fake.calls == []
    assert "secrets push" in capsys.readouterr().err


def test_dry_run_pulls_nothing(site, monkeypatch) -> None:
    monkeypatch.setattr(h, "read_host", lambda host, c: in_sync_state(c))
    fake = FakeHost(changed=[DJANGO])
    assert run_deploy(site, fake, dry_run=True) == 0
    assert fake.calls == []


def test_a_unit_that_isnt_active_fails(site, monkeypatch) -> None:
    monkeypatch.setattr(h, "read_host", lambda host, c: in_sync_state(c))
    assert run_deploy(site, FakeHost(states="failed")) == 1


def test_tag_and_a_stale_render_are_refused(site, rendered, monkeypatch, capsys) -> None:
    monkeypatch.setattr(h, "read_host", lambda *a: pytest.fail("touched the host"))
    assert run_deploy(site, FakeHost(), tag="v9") == 2
    assert "image_tag" in capsys.readouterr().err
    rendered["check"] = 1
    assert run_deploy(site, FakeHost()) == 1
    assert "not touching the host" in capsys.readouterr().err


def test_bare_dk_env_deploys(site, monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(d, "deploy", lambda config, env, info, **k: seen.append((env, k)) or 0)
    ns = argparse.Namespace(env_command=None, implicit_compose_argv=[], dry_run=False, yes=True, tag=None)
    assert backend.handle(ns, site, "staging", INFO) == 0
    assert seen == [("staging", {"yes": True, "dry_run": False, "tag": None})]
    # An explicit `up` is still refused, with the hint.
    ns.implicit_compose_argv = ["up", "-d"]
    assert backend.handle(ns, site, "staging", INFO) == 2


def test_a_unit_that_is_down_is_started(site, committed, monkeypatch, capsys) -> None:
    """As compose's `up -d` starts stopped services: django (inactive after a failed migrate)
    is started, with migrate/collectstatic reset first so they run again."""
    monkeypatch.setattr(h, "read_host", lambda host, c: in_sync_state(c))
    units, _ = h.stack_units(committed)
    answers = iter(
        [
            " ".join("inactive" if u == "django.service" else "active" for u in units),
            " ".join("active" for _ in units),
        ]
    )

    class Host(FakeHost):
        def __call__(self, argv, *, input=None, tty=False):
            if "is-active" in argv[-1]:
                self.calls.append((argv[-1], input))
                return subprocess.CompletedProcess(argv, 0, next(answers).replace(" ", "\n").encode(), b"")
            return super().__call__(argv, input=input, tty=tty)

    fake = Host()
    assert run_deploy(site, fake) == 0
    start = next(cmd for cmd, _ in fake.calls if "systemctl --user start" in cmd)
    assert "reset-failed" in start and "migrate.service" in start and "collectstatic.service" in start
    assert start.split("start ", 1)[1].split()[0] == "django.service"
    assert "up to date" in capsys.readouterr().err
