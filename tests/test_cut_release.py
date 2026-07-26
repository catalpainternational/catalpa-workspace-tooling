"""Tests for dk cut-release / next-branch plan building (temp git repos)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.cut_release import (
    CutReleaseError,
    build_beta_plan,
    build_final_plan,
    build_next_branch_plan,
    cut_release_final,
)
from catalpa_tooling.dk_parser import build_dk_parser
from tests.helpers import write_minimal_tooling_tree


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(path: Path, *, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README").write_text("x\n", encoding="utf-8")
    _git(path, "add", "README")
    _git(path, "commit", "-m", "init")
    current = subprocess.run(
        ["git", "-C", str(path), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if current != "main":
        _git(path, "branch", "-m", current, "main")
    if branch != "main":
        _git(path, "checkout", "-b", branch)
    return path


@pytest.fixture
def tooling_repo(tmp_path: Path) -> Path:
    write_minimal_tooling_tree(tmp_path)
    for env in ("local", "staging"):
        d = tmp_path / "docker" / "envs" / env
        d.mkdir(parents=True)
        (d / "info.yaml").write_text(f"name: {env}\n", encoding="utf-8")
    return tmp_path


def test_dk_parser_cut_release_final(
    tooling_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tooling_repo)
    config = load_project_config(tooling_repo)
    parser = build_dk_parser(config)
    ns = parser.parse_args(["cut-release", "final", "-C", "bero", "--execute", "-y"])
    assert ns.dk_command == "cut-release"
    assert ns.cut_release_command == "final"
    assert ns.submodule_path == "bero"
    assert ns.execute is True
    assert ns.yes is True


def test_dk_parser_cut_release_beta(
    tooling_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tooling_repo)
    config = load_project_config(tooling_repo)
    parser = build_dk_parser(config)
    ns = parser.parse_args(["cut-release", "beta", "3", "--tag", "v7.4.1.beta.9"])
    assert ns.cut_release_command == "beta"
    assert ns.beta_w == 3
    assert ns.tag == "v7.4.1.beta.9"


def test_dk_parser_next_branch(
    tooling_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tooling_repo)
    config = load_project_config(tooling_repo)
    parser = build_dk_parser(config)
    ns = parser.parse_args(["next-branch", "hotfix", "--set-default", "--execute"])
    assert ns.dk_command == "next-branch"
    assert ns.spec == "hotfix"
    assert ns.set_default is True
    assert ns.execute is True


def test_dk_parser_final_rejects_tag(
    tooling_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tooling_repo)
    config = load_project_config(tooling_repo)
    parser = build_dk_parser(config)
    with pytest.raises(SystemExit):
        parser.parse_args(["cut-release", "final", "--tag", "v1.0"])


def test_plan_final(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    plan = build_final_plan(repo=repo)
    assert plan.mode == "final"
    assert plan.release_tag == "v7.4.1"
    assert plan.next_branch is None
    assert plan.from_branch == "dev-7.4.1"
    assert plan.set_default is False


def test_plan_beta_auto_w(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    _git(repo, "tag", "-a", "v7.4.1.beta.1", "-m", "b1")
    _git(repo, "tag", "-a", "v7.4.1.beta.2", "-m", "b2")
    plan = build_beta_plan(repo=repo, beta_w=None, tag_override=None)
    assert plan.mode == "beta"
    assert plan.beta_tag == "v7.4.1.beta.3"
    assert plan.next_branch is None


def test_plan_beta_explicit_w(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    plan = build_beta_plan(repo=repo, beta_w=5, tag_override=None)
    assert plan.beta_tag == "v7.4.1.beta.5"


def test_plan_beta_non_dev_requires_tag(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="feature/foo")
    with pytest.raises(CutReleaseError, match="pass --tag"):
        build_beta_plan(repo=repo, beta_w=None, tag_override=None)
    plan = build_beta_plan(repo=repo, beta_w=None, tag_override="v7.4.1.beta.1")
    assert plan.beta_tag == "v7.4.1.beta.1"
    assert plan.from_branch == "feature/foo"


def test_plan_beta_refuses_detached(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    _git(repo, "tag", "-a", "v7.4.1", "-m", "v")
    _git(repo, "checkout", "v7.4.1")
    with pytest.raises(CutReleaseError, match="named branch"):
        build_beta_plan(repo=repo, beta_w=None, tag_override=None)


def test_plan_beta_rejects_w_and_tag(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    with pytest.raises(CutReleaseError, match="not both"):
        build_beta_plan(repo=repo, beta_w=3, tag_override="v7.4.1.beta.3")


def test_plan_next_branch_from_detached_tag(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app")
    _git(repo, "tag", "-a", "v7.4.1", "-m", "v7.4.1")
    _git(repo, "checkout", "v7.4.1")
    plan = build_next_branch_plan(repo=repo, spec="minor", set_default=True)
    assert plan.mode == "next-branch"
    assert plan.release_tag is None
    assert plan.next_branch == "dev-7.5"
    assert plan.set_default is True
    assert plan.prior_dev_branch == "dev-7.4.1"


def test_plan_next_branch_from_main_at_tag(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app")
    _git(repo, "tag", "-a", "v7.4.1", "-m", "v7.4.1")
    # still on main, which points at the tagged commit
    plan = build_next_branch_plan(repo=repo, spec="hotfix", set_default=False)
    assert plan.next_branch == "dev-7.4.2"


def test_plan_next_branch_explicit_dev(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app")
    _git(repo, "tag", "-a", "v7.4.1", "-m", "v")
    plan = build_next_branch_plan(repo=repo, spec="dev-8.0", set_default=False)
    assert plan.next_branch == "dev-8.0"


def test_plan_next_branch_rejects_untagged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    with pytest.raises(CutReleaseError, match="final v"):
        build_next_branch_plan(repo=repo, spec="hotfix", set_default=False)


def test_cut_release_final_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    rc = cut_release_final(repo_root=repo, execute=False)
    assert rc == 0
    err = capsys.readouterr().err
    assert "mode:          final" in err
    assert "release_tag:   v7.4.1" in err


def test_execute_refuses_dirty_without_allow(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    (repo / "README").write_text("dirty\n", encoding="utf-8")
    rc = cut_release_final(repo_root=repo, execute=True, yes=True)
    assert rc == 1


def test_execute_allow_dirty_warns_then_fails_on_missing_remote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    (repo / "README").write_text("dirty\n", encoding="utf-8")
    rc = cut_release_final(repo_root=repo, execute=True, yes=True, allow_dirty=True)
    captured = capsys.readouterr()
    assert "WARNING: proceeding with a dirty working tree" in captured.err
    assert rc != 0
