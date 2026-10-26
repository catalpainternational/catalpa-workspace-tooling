"""Tests for ``dk worktree claude-hook`` (Claude Code's WorktreeCreate / WorktreeRemove hooks)."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.worktree_claude_hook import cmd_claude_hook, remote_default_branch
from catalpa_tooling.worktree_overlay import load_worktree_overlay
from tests.helpers import git_init_commit, write_dev_env, write_minimal_tooling_tree


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, isolated_tooling: None, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A main checkout whose ``origin`` (a bare repo) is one commit ahead of its HEAD."""
    # remove stops the stack; don't shell out to a real `docker compose down`
    monkeypatch.setattr(
        "catalpa_tooling.worktree._compose",
        lambda *a, **k: subprocess.CompletedProcess(list(a), 0),
    )
    main = tmp_path / "main"
    main.mkdir()
    write_minimal_tooling_tree(main)
    write_dev_env(main)
    git_init_commit(main)
    _git(main, "branch", "-M", "trunk")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "--initial-branch=trunk", str(origin))
    _git(main, "remote", "add", "origin", str(origin))
    (main / "later.txt").write_text("on origin only\n", encoding="utf-8")
    _git(main, "add", "later.txt")
    _git(main, "commit", "-m", "later")
    _git(main, "push", "-q", "origin", "trunk")
    _git(main, "reset", "-q", "--hard", "HEAD~1")  # the human's checkout is behind
    _git(main, "update-ref", "-d", "refs/remotes/origin/trunk")  # and has not fetched
    return main


def _hook(repo: Path, event: str, payload: dict) -> int:
    return cmd_claude_hook(event, load_project_config(repo), io.StringIO(json.dumps(payload)))


def test_remote_default_branch_asks_the_remote(repo: Path) -> None:
    assert remote_default_branch(repo) == "trunk"


def test_create_cuts_from_fetched_origin_and_prints_only_the_path(
    repo: Path, capfd: pytest.CaptureFixture
) -> None:
    assert _hook(repo, "create", {"name": "Bold-Oak"}) == 0

    out, err = capfd.readouterr()
    wt = repo / ".worktrees" / "bold_oak"
    assert out == f"{wt.resolve()}\n", "stdout must be the path alone; dk output goes to stderr"
    assert "git worktree add" in err
    assert (wt / "later.txt").is_file(), "cut from the main checkout's stale HEAD, not origin"
    overlay = load_worktree_overlay(wt)
    assert overlay is not None
    assert overlay.branch == "worktree/bold_oak"
    assert overlay.base_ref == "origin/trunk"
    assert not (repo / ".claude" / "worktrees").exists()


def test_create_without_a_name_fails(repo: Path) -> None:
    assert _hook(repo, "create", {}) == 1


def test_remove_deletes_a_merged_branch(repo: Path) -> None:
    _git(repo, "pull", "-q", "--ff-only", "origin", "trunk")  # trunk now contains the base
    assert _hook(repo, "create", {"name": "tidy"}) == 0
    wt = repo / ".worktrees" / "tidy"

    assert _hook(repo, "remove", {"worktree_path": str(wt)}) == 0
    assert not wt.exists()
    assert _git(repo, "branch", "--list", "worktree/tidy") == ""


def test_remove_keeps_a_branch_with_unmerged_work(
    repo: Path, capfd: pytest.CaptureFixture
) -> None:
    assert _hook(repo, "create", {"name": "wip"}) == 0
    wt = repo / ".worktrees" / "wip"
    (wt / "new.txt").write_text("work\n", encoding="utf-8")
    _git(wt, "add", "new.txt")
    _git(wt, "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-m", "work")
    capfd.readouterr()

    assert _hook(repo, "remove", {"worktree_path": str(wt)}) == 0
    assert not wt.exists()
    assert _git(repo, "branch", "--list", "worktree/wip") != ""
    assert "Kept branch worktree/wip" in capfd.readouterr().err


def test_remove_refuses_a_path_outside_worktrees(repo: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _hook(repo, "remove", {"worktree_path": str(elsewhere)}) == 1
    assert elsewhere.is_dir()


def test_bad_hook_input_fails(repo: Path) -> None:
    config = load_project_config(repo)
    assert cmd_claude_hook("create", config, io.StringIO("not json")) == 1
    assert cmd_claude_hook("remove", config, io.StringIO("[]")) == 1
