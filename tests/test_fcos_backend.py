"""dk's stack commands on a ``runtime: quadlet`` env (``fcos/backend.py``), against the site
fixture's golden render, with SSH faked."""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.fcos import backend

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"


@pytest.fixture
def site(tmp_path: Path):
    root = tmp_path / "site"
    shutil.copytree(FIXTURES / "site", root)
    shutil.copytree(root / "expected", root / "docker" / "envs" / "staging" / "generated" / "fcos")
    return load_project_config(root)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, input=None, tty=False):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    @property
    def remote(self) -> list[str]:
        """The last call's remote command, split."""
        return shlex.split(self.calls[-1][-1])

    @property
    def ssh_flags(self) -> list[str]:
        return self.calls[-1][:-2]


def run(config, *argv: str, tty: bool = False, **ns_extra) -> tuple[int, Recorder]:
    r = Recorder()
    if argv and argv[0] == "manage":
        ns = argparse.Namespace(env_command="manage", manage_args=list(argv[1:]), dry_run=False, **ns_extra)
    else:
        ns = argparse.Namespace(env_command=None, implicit_compose_argv=list(argv), dry_run=False, **ns_extra)
    info = {"docker_host": "ssh://deploy@staging.site.example/run/user/1001/podman/podman.sock"}
    return backend.handle(ns, config, "staging", info, runner=r, tty=tty), r


def test_logs_follow_selected_services(site) -> None:
    rc, r = run(site, "logs", "-f", "--tail", "50", "django", "db", tty=True)
    assert rc == 0
    assert r.calls[-1][-2] == "deploy@staging.site.example"
    assert "-t" in r.ssh_flags
    assert r.remote == [
        "journalctl", "--user", "--no-pager", "-u", "django.service", "-u", "db.service",
        "-n", "50", "-f",
    ]  # fmt: skip


def test_logs_default_all_services_no_tty_when_not_following(site) -> None:
    rc, r = run(site, "logs", tty=True)
    assert rc == 0
    assert "-t" not in r.ssh_flags
    assert [a for a in r.remote if a.endswith(".service")] == [
        "caddy.service", "collectstatic.service", "db.service", "django.service", "migrate.service",
    ]  # fmt: skip


def test_exec_and_manage(site) -> None:
    rc, r = run(site, "exec", "-T", "-u", "postgres", "db", "psql", "-c", "select 1")
    assert rc == 0
    assert r.remote == ["podman", "exec", "-i", "-u", "postgres", "db", "psql", "-c", "select 1"]
    rc, r = run(site, "exec", "django", "sh", tty=True)
    assert r.remote == ["podman", "exec", "-i", "-t", "django", "sh"]
    assert "-t" in r.ssh_flags
    rc, r = run(site, "manage", "migrate", "--plan", user=None)
    assert rc == 0
    assert r.remote[:3] == ["podman", "exec", "-i"]
    assert r.remote[-4:] == ["django", "./manage.py", "migrate", "--plan"]
    rc, r = run(site, "manage", "shell", user="root")
    assert ["-u", "root"] == r.remote[3:5]


def test_ps(site) -> None:
    rc, r = run(site, "ps")
    assert rc == 0
    cmd = r.calls[-1][-1]
    assert cmd.startswith("systemctl --user --no-pager --legend=false list-units --all ")
    assert "caddy.socket" in cmd and "migrate.service" in cmd and "podman ps -a" in cmd


def test_unit_verbs(site) -> None:
    rc, r = run(site, "restart", "django")
    assert (rc, r.calls[-1][-1]) == (0, "systemctl --user restart django.service")
    rc, r = run(site, "stop")
    # The socket first, or the next request starts the edge again.
    assert r.calls[-1][-1] == (
        "systemctl --user stop caddy.socket caddy.service db.service django.service"
    )
    rc, r = run(site, "start")
    assert r.calls[-1][-1].endswith("django.service caddy.socket")
    rc, r = run(site, "restart")
    assert r.calls[-1][-1] == "systemctl --user restart caddy.service db.service django.service"


@pytest.mark.parametrize(
    "argv",
    [["up", "-d"], ["down"], ["pull"], ["build"], ["run", "django", "sh"], ["config"]],
)
def test_compose_only_verbs_are_refused_without_ssh(site, argv, capsys) -> None:
    rc, r = run(site, *argv)
    assert rc == 2 and r.calls == []
    assert "Available: logs, ps, exec, manage" in capsys.readouterr().err


def test_unknown_service_and_bad_usage(site, capsys) -> None:
    rc, r = run(site, "logs", "nope")
    assert rc == 2 and r.calls == []
    assert "not a service of staging: nope" in capsys.readouterr().err
    rc, r = run(site, "exec", "--privileged", "django", "sh")
    assert rc == 2 and r.calls == []
    rc, r = run(site, "exec", "django")
    assert rc == 2 and r.calls == []
    rc, r = run(site, "restart", "--timeout", "5")
    assert rc == 2 and r.calls == []


def test_without_a_committed_render(site, capsys) -> None:
    shutil.rmtree(site.deploy_envs_dir / "staging" / "generated")
    rc, r = run(site, "ps")
    assert rc == 1 and r.calls == []
    assert "fcos render" in capsys.readouterr().err


def test_dk_routes_a_quadlet_env_here(site) -> None:
    from catalpa_tooling.env_handlers import handle_env_command

    ns = argparse.Namespace(
        env_name="staging", env_command=None, implicit_compose_argv=["up"], dry_run=False, tag=None
    )
    assert handle_env_command(ns, site) == 2
