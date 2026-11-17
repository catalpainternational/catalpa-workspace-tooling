#!/usr/bin/env python3
"""Claude Code ``WorktreeCreate`` / ``WorktreeRemove`` hook for this repo.

Claude Code runs this instead of its own ``git worktree add``, so ``claude --worktree``,
EnterWorktree and subagents with ``isolation: worktree`` get a worktree under
``<main checkout>/.worktrees/<slug>`` (gitignored), cut from a freshly fetched
``origin/<default branch>``, rather than one under ``.claude/worktrees/``.

This repo is not a dk project, so it can't use ``dk worktree claude-hook``; this is the
same idea with plain git and only the standard library.

Usage (from .claude/settings.json): ``worktree.py create`` or ``worktree.py remove``,
with the hook's JSON payload on stdin. Create must print the worktree's absolute path as
the last line of stdout, so all git output goes to stderr. Remove gets ``worktree_path``.
Claude Code never deletes the branch of a worktree a hook made, so remove deletes it here
when it is merged into origin/<default branch> or fully pushed, and keeps it otherwise.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

WORKTREES_DIRNAME = ".worktrees"
BRANCH_PREFIX = "worktree/"


def log(msg: str) -> None:
    print(f"worktree hook: {msg}", file=sys.stderr)


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if proc.stdout:
        sys.stderr.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if check and proc.returncode != 0:
        raise SystemExit(f"worktree hook: git {' '.join(args)} failed ({proc.returncode})")
    return proc


def git_out(*args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def main_checkout(start: Path) -> Path:
    """The main checkout, even when Claude Code runs us from inside a worktree."""
    common = git_out("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=start)
    if not common:
        raise SystemExit(f"worktree hook: {start} is not inside a git repository")
    return Path(common).parent


def default_branch(root: Path) -> str:
    ref = git_out("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=root)
    return ref.removeprefix("origin/") if ref else "main"


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    if not slug:
        raise SystemExit(f"worktree hook: can't make a directory name from {name!r}")
    return slug


def create(payload: dict, root: Path) -> int:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise SystemExit("worktree hook: create input has no name")
    slug = slugify(name)
    path = root / WORKTREES_DIRNAME / slug

    if (path / ".git").exists():
        log(f"reusing existing worktree {path}")
        print(path.resolve())
        return 0

    base_branch = default_branch(root)
    base = f"origin/{base_branch}"
    git("fetch", "origin", base_branch, cwd=root)

    branch = f"{BRANCH_PREFIX}{slug}"
    path.parent.mkdir(exist_ok=True)
    if git_out("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=root):
        log(f"branch {branch} already exists; checking it out")
        git("worktree", "add", str(path), branch, cwd=root)
    else:
        # --no-track: the new branch must not push to the default branch by accident.
        git("worktree", "add", "--no-track", "-b", branch, str(path), base, cwd=root)
    print(path.resolve())
    return 0


def remove(payload: dict, root: Path) -> int:
    raw = str(payload.get("worktree_path") or "").strip()
    if not raw:
        raise SystemExit("worktree hook: remove input has no worktree_path")
    path = Path(raw).resolve()
    if path.parent != (root / WORKTREES_DIRNAME).resolve():
        log(f"{path} is not under {root / WORKTREES_DIRNAME}; leaving it alone")
        return 0

    branch = git_out("rev-parse", "--abbrev-ref", "HEAD", cwd=path) if path.exists() else ""
    # Claude Code only asks to remove after the user/agent chose to discard changes.
    git("worktree", "remove", "--force", str(path), cwd=root, check=False)
    git("worktree", "prune", cwd=root, check=False)
    if branch and branch != "HEAD":
        delete_branch_if_safe(branch, root)
    return 0


def delete_branch_if_safe(branch: str, root: Path) -> None:
    """Delete ``branch`` if it is merged into origin/<default>, or ``git branch -d`` allows it.

    ``git branch -d`` alone checks against the main checkout's HEAD, which may be any branch.
    Squash-merged branches aren't detected and are kept.
    """
    base = f"origin/{default_branch(root)}"
    merged = subprocess.run(
        ["git", "merge-base", "--is-ancestor", branch, base], cwd=root, capture_output=True
    ).returncode == 0
    flag = "-D" if merged else "-d"
    if git("branch", flag, branch, cwd=root, check=False).returncode != 0:
        log(f"kept branch {branch} (not merged into {base} and not fully pushed)")


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("create", "remove"):
        print("usage: worktree.py create|remove < hook-input.json", file=sys.stderr)
        return 2
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        log(f"bad hook input: {exc}")
        return 1
    cwd = Path(str(payload.get("cwd") or ".")).resolve()
    root = main_checkout(cwd if cwd.exists() else Path.cwd())
    return create(payload, root) if argv[1] == "create" else remove(payload, root)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
