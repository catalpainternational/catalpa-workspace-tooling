"""Tests for dk cut-release plan building (temp git repos)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from catalpa_tooling.cut_release import CutReleaseError, build_cut_release_plan, cut_release
from catalpa_tooling.config import load_project_config
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
    # rename default branch to main if needed
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


def test_dk_parser_cut_release(tooling_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tooling_repo)
    config = load_project_config(tooling_repo)
    parser = build_dk_parser(config)
    ns = parser.parse_args(
        [
            "cut-release",
            "--bump",
            "hotfix",
            "--submodule",
            "bero",
            "--execute",
            "--set-default",
            "--image-env",
            "staging",
            "--pin-submodule",
            "bero=v7.4.1",
        ]
    )
    assert ns.dk_command == "cut-release"
    assert ns.bump == "hotfix"
    assert ns.submodule == "bero"
    assert ns.execute is True
    assert ns.set_default is True
    assert ns.image_env == "staging"
    assert ns.pin_submodule == ["bero=v7.4.1"]


def test_plan_mode_a_hotfix(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    plan = build_cut_release_plan(
        repo=repo,
        bump="hotfix",
        beta=False,
        beta_w=None,
        tag_override=None,
        next_branch_override=None,
        set_default=True,
        pin_submodules=None,
        image_env="staging",
        allow_prod_beta=False,
    )
    assert plan.mode == "release"
    assert plan.release_tag == "v7.4.1"
    assert plan.next_branch == "dev-7.4.2"
    assert plan.from_branch == "dev-7.4.1"
    assert plan.image_tag_value == "v7.4.1"
    assert plan.set_default is True


def test_plan_mode_a_minor_major(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    minor = build_cut_release_plan(
        repo=repo,
        bump="minor",
        beta=False,
        beta_w=None,
        tag_override=None,
        next_branch_override=None,
        set_default=False,
        pin_submodules=None,
        image_env=None,
        allow_prod_beta=False,
    )
    assert minor.next_branch == "dev-7.5"
    major = build_cut_release_plan(
        repo=repo,
        bump="major",
        beta=False,
        beta_w=None,
        tag_override=None,
        next_branch_override=None,
        set_default=False,
        pin_submodules=None,
        image_env=None,
        allow_prod_beta=False,
    )
    assert major.next_branch == "dev-8.0"


def test_plan_mode_b_from_tag(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app")
    _git(repo, "tag", "-a", "v7.4.1", "-m", "v7.4.1")
    # detach onto the tag
    _git(repo, "checkout", "v7.4.1")
    plan = build_cut_release_plan(
        repo=repo,
        bump="minor",
        beta=False,
        beta_w=None,
        tag_override=None,
        next_branch_override=None,
        set_default=True,
        pin_submodules=None,
        image_env=None,
        allow_prod_beta=False,
    )
    assert plan.mode == "next-branch"
    assert plan.release_tag is None
    assert plan.next_branch == "dev-7.5"


def test_plan_mode_c_beta_auto_w(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    _git(repo, "tag", "-a", "v7.4.1.beta.1", "-m", "b1")
    _git(repo, "tag", "-a", "v7.4.1.beta.2", "-m", "b2")
    plan = build_cut_release_plan(
        repo=repo,
        bump=None,
        beta=True,
        beta_w=None,
        tag_override=None,
        next_branch_override=None,
        set_default=False,
        pin_submodules=None,
        image_env="staging",
        allow_prod_beta=False,
    )
    assert plan.mode == "beta"
    assert plan.beta_tag == "v7.4.1.beta.3"
    assert plan.next_branch is None
    assert plan.image_tag_value == "v7.4.1.beta.3"


def test_plan_mode_c_refuses_prod_beta(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-2.9")
    with pytest.raises(CutReleaseError, match="prod"):
        build_cut_release_plan(
            repo=repo,
            bump=None,
            beta=True,
            beta_w=None,
            tag_override=None,
            next_branch_override=None,
            set_default=False,
            pin_submodules=None,
            image_env="prod",
            allow_prod_beta=False,
        )


def test_plan_beta_rejects_bump(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-2.9")
    with pytest.raises(CutReleaseError, match="cannot be combined"):
        build_cut_release_plan(
            repo=repo,
            bump="hotfix",
            beta=True,
            beta_w=None,
            tag_override=None,
            next_branch_override=None,
            set_default=False,
            pin_submodules=None,
            image_env=None,
            allow_prod_beta=False,
        )


def test_cut_release_dry_run(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    rc = cut_release(repo_root=repo, bump="hotfix", execute=False)
    assert rc == 0


def test_execute_refuses_dirty_without_allow(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    (repo / "README").write_text("dirty\n", encoding="utf-8")
    rc = cut_release(repo_root=repo, bump="hotfix", execute=True, yes=True)
    assert rc == 1


def test_execute_allow_dirty_warns_then_fails_on_missing_remote(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With --allow-dirty, clean-tree check is skipped (fetch may still fail without origin)."""
    repo = _init_repo(tmp_path / "app", branch="dev-7.4.1")
    (repo / "README").write_text("dirty\n", encoding="utf-8")
    rc = cut_release(
        repo_root=repo, bump="hotfix", execute=True, yes=True, allow_dirty=True
    )
    captured = capsys.readouterr()
    assert "WARNING: proceeding with a dirty working tree" in captured.err
    # No origin remote in temp repo → fetch fails after dirty bypass
    assert rc != 0
