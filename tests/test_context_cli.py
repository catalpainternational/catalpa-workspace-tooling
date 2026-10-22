"""Tests for ``dk context`` (``context_cli.py``)."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from catalpa_tooling.config import load_project_config
from catalpa_tooling.context_cli import build_context, cmd_context
from catalpa_tooling.dk_cli import _parse_dk
from catalpa_tooling.dk_parser import build_dk_parser
from catalpa_tooling.worktree import worktree_create
from tests.helpers import git_init_commit, write_dev_env, write_minimal_tooling_tree


def _write_remote_env(repo: Path, name: str = "staging") -> None:
    env_dir = repo / "docker" / "envs" / name
    env_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "docker_host": "ssh://root@203.0.113.7",
        "site_origin": "https://minimal-staging.example.org",
        "redirect_origins": ["https://www.minimal-staging.example.org"],
    }
    (env_dir / "info.yaml").write_text(yaml.safe_dump(info), encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path, isolated_tooling: None) -> Path:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    _write_remote_env(tmp_path)
    return tmp_path


@pytest.fixture
def no_docker(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record commands run by ``context_cli``; fail on anything but git."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] != "git":
            raise AssertionError(f"unexpected command: {cmd}")
        return subprocess.run(cmd, **{k: v for k, v in kwargs.items() if k != "print_cmd"})

    monkeypatch.setattr("catalpa_tooling.context_cli.run_cmd", fake_run)
    monkeypatch.setattr(
        "catalpa_tooling.managed_deploy_env.load_managed_deploy_context",
        lambda *a, **k: pytest.fail("dk context must not load credentials"),
    )
    return calls


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**{"context_env": None, "json": True, "status": False, **kw})


def test_main_checkout_lists_every_env(project: Path, no_docker: list) -> None:
    payload = build_context(load_project_config(project))

    assert payload["schema"] == 1
    assert payload["checkout"] == "main"
    assert payload["worktree"] is None
    assert payload["web_service"] == "web"
    envs = {e["name"]: e for e in payload["envs"]}
    assert set(envs) == {"dev", "staging"}

    dev = envs["dev"]
    assert dev["location"] == "local"
    assert dev["compose_project"] == "app_compose_dev"
    assert dev["origins"] == [
        "https://minimal-dev.localdev.temp.build",
        "https://admin.minimal-dev.localdev.temp.build",
        "https://stats.minimal-dev.localdev.temp.build",
    ]
    assert dev["local_proxy"] is True
    assert dev["worktree_overlay"] is False
    assert "status" not in dev

    staging = envs["staging"]
    assert staging["location"] == "remote"
    # compose on a remote host uses the bare default, not {default}_{env}
    assert staging["compose_project"] == "app_compose"
    assert staging["redirect_origins"] == ["https://www.minimal-staging.example.org"]
    assert staging["local_proxy"] is False
    assert no_docker == []


def test_django_extra_origins_are_listed(project: Path, no_docker: list) -> None:
    info_path = project / "docker" / "envs" / "dev" / "info.yaml"
    info = yaml.safe_load(info_path.read_text())
    info["env"]["django_extra_origins"] = "getbero.localdev.temp.build,admin.minimal-dev.localdev.temp.build"
    info_path.write_text(yaml.safe_dump(info), encoding="utf-8")

    payload = build_context(load_project_config(project), env="dev")
    origins = payload["envs"][0]["origins"]
    assert origins[-1] == "https://getbero.localdev.temp.build"
    assert origins.count("https://admin.minimal-dev.localdev.temp.build") == 1


def test_env_narrows_and_unknown_env_errors(
    project: Path, no_docker: list, capsys: pytest.CaptureFixture[str]
) -> None:
    config = load_project_config(project)
    assert [e["name"] for e in build_context(config, env="staging")["envs"]] == ["staging"]

    assert cmd_context(_ns(context_env="nope"), config) == 1
    assert "unknown env 'nope'" in capsys.readouterr().err


def test_json_stdout_is_only_json(
    project: Path, no_docker: list, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cmd_context(_ns(), load_project_config(project)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project"] == "minimal"


def test_inside_worktree_reports_overlay_and_base(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    git_init_commit(project)
    main_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=project, check=True, capture_output=True, text=True,
    ).stdout.strip()
    main_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert worktree_create(load_project_config(project), slug="ctx", dry_run=False, seed=False) == 0
    capsys.readouterr()

    wt_config = load_project_config(project / ".worktrees" / "ctx")
    payload = build_context(wt_config)

    assert payload["checkout"] == "worktree"
    wt = payload["worktree"]
    assert wt["slug"] == "ctx"
    assert wt["branch"] == "worktree/ctx"
    assert wt["base_ref"] == main_branch
    assert wt["base_commit"] == main_commit
    assert wt["head_commit"] == main_commit

    envs = {e["name"]: e for e in payload["envs"]}
    dev = envs["dev"]
    assert dev["worktree_overlay"] is True
    assert dev["compose_project"] == "app_compose_dev_ctx"
    assert dev["origins"][0] == "https://minimal-dev-ctx.localdev.temp.build"
    assert "https://admin.minimal-dev-ctx.localdev.temp.build" in dev["origins"]
    # only the overlay's base env is remapped
    assert envs["staging"]["worktree_overlay"] is False
    assert envs["staging"]["compose_project"] == "app_compose"


def test_status_lists_containers_for_local_envs_only(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        out = "web\trunning\ndb\trunning\nmigrate\texited\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr("catalpa_tooling.context_cli.run_cmd", fake_run)
    envs = {e["name"]: e for e in build_context(load_project_config(project), status=True)["envs"]}

    assert envs["dev"]["status"] == {"running": ["db", "web"], "stopped": ["migrate"]}
    assert "status" not in envs["staging"]
    assert len(calls) == 1
    assert "label=com.docker.compose.project=app_compose_dev" in calls[0]


def test_status_reports_engine_errors(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.context_cli.run_cmd",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="Cannot connect to the Docker daemon"
        ),
    )
    payload = build_context(load_project_config(project), env="dev", status=True)
    assert payload["envs"][0]["status"] == {"error": "Cannot connect to the Docker daemon"}


def test_bad_info_yaml_is_reported_per_env(project: Path, no_docker: list) -> None:
    (project / "docker" / "envs" / "staging" / "info.yaml").write_text("- not a mapping\n")
    envs = {e["name"]: e for e in build_context(load_project_config(project))["envs"]}
    assert "expected a mapping" in envs["staging"]["error"]
    assert envs["dev"]["location"] == "local"


def test_parser_accepts_dk_context(project: Path) -> None:
    ns = build_dk_parser(load_project_config(project)).parse_args(
        ["context", "--env", "dev", "--json", "--status"]
    )
    assert ns.dk_command == "context"
    assert (ns.context_env, ns.json, ns.status) == ("dev", True, True)


@pytest.mark.parametrize(
    ("name", "argv"),
    [("context", ["context", "--json"]), ("digoc", ["digoc", "droplets", "list"])],
)
def test_env_named_like_a_top_level_command_does_not_shadow_it(
    project: Path, name: str, argv: list[str]
) -> None:
    (project / "docker" / "envs" / name).mkdir()
    (project / "docker" / "envs" / name / "info.yaml").write_text("{}\n")
    config = load_project_config(project)
    ns = _parse_dk(config, argv, build_dk_parser(config))
    assert ns.dk_command == name
