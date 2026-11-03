"""``dk worktree claude-hook`` — Claude Code's ``WorktreeCreate`` / ``WorktreeRemove`` hooks.

When a project's ``.claude/settings.json`` configures these hooks (setup-agents does, with the
``worktrees`` rule), Claude Code runs them instead of its own ``git worktree add``. So
``claude --worktree``, EnterWorktree and subagents with ``isolation: worktree`` get a dk
worktree under ``.worktrees/<slug>``, not a bare one under ``.claude/worktrees/``.

The protocol is JSON on stdin. Create must print the worktree's absolute path as the last line
of stdout, so everything else it runs (including git's own output) goes to stderr. Remove gets
the ``worktree_path`` create printed. Claude Code never deletes the branch of a worktree a hook
made, so remove deletes it here when git agrees it is merged, and keeps it otherwise.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.worktree import (
    _git,
    resolve_parent_repo_root,
    worktree_create,
    worktree_remove,
)
from catalpa_tooling.worktree_overlay import (
    WorktreeOverlayError,
    load_worktree_overlay,
    sanitize_worktree_slug,
    worktrees_dir,
)

HOOK_EVENTS = ("create", "remove")


def read_hook_payload(stream: IO[str]) -> dict[str, Any]:
    """The hook's stdin JSON object; raises ``ValueError`` when it is not one."""
    try:
        data = json.loads(stream.read() or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"hook input is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("hook input is not a JSON object")
    return data


@contextlib.contextmanager
def _stdout_to_stderr() -> Iterator[IO[str]]:
    """Send stdout to stderr, at fd 1 (subprocesses) and ``sys.stdout``; yield the real stdout."""
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(2, 1)
    real = os.fdopen(saved, "w", encoding="utf-8", closefd=False)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            yield real
    finally:
        sys.stdout.flush()
        real.flush()
        os.dup2(saved, 1)
        real.close()
        os.close(saved)


def remote_default_branch(repo_root: Path) -> str | None:
    """The default branch ``origin`` reports now, falling back to the local ``origin/HEAD``.

    The local ``origin/HEAD`` is only what it was at clone time (or the last
    ``remote set-head``), so it is asked of the remote first.
    """
    live = _git("ls-remote", "--symref", "origin", "HEAD", cwd=repo_root, check=False, capture=True)
    if live.returncode == 0:
        for line in live.stdout.splitlines():
            ref, _, name = line.partition("\t")
            if name.strip() == "HEAD" and ref.startswith("ref: refs/heads/"):
                return ref.removeprefix("ref: refs/heads/").strip() or None
    cached = _git(
        "symbolic-ref",
        "--short",
        "refs/remotes/origin/HEAD",
        cwd=repo_root,
        check=False,
        capture=True,
    )
    if cached.returncode == 0 and cached.stdout.strip().startswith("origin/"):
        return cached.stdout.strip().removeprefix("origin/") or None
    return None


def claude_hook_create(config: ProjectConfig, payload: dict[str, Any]) -> int:
    """Create ``.worktrees/<name>`` from ``origin/<default branch>``, without seed or stack."""
    name = str(payload.get("name") or "").strip()
    if not name:
        print("dk worktree claude-hook create: hook input has no name", file=sys.stderr)
        return 1
    try:
        slug = sanitize_worktree_slug(name)
    except WorktreeOverlayError as exc:
        print(f"dk worktree claude-hook create: {exc}", file=sys.stderr)
        return 1

    main_root = config.repo_root.resolve()
    with _stdout_to_stderr() as stdout:
        branch = remote_default_branch(main_root)
        if branch is None:
            print(
                "dk worktree claude-hook create: cannot tell origin's default branch; "
                "cutting from HEAD",
                file=sys.stderr,
            )
            base = "HEAD"
        else:
            if _git("fetch", "origin", branch, cwd=main_root, check=False).returncode != 0:
                print(
                    f"dk worktree claude-hook create: fetch of {branch} failed; "
                    f"cutting from the local origin/{branch}",
                    file=sys.stderr,
                )
            base = f"origin/{branch}"
        rc = worktree_create(config, slug=slug, base_branch=base, seed=False, bring_up=False)
        if rc != 0:
            return rc
        print(str((worktrees_dir(main_root) / slug).resolve()), file=stdout)
    return 0


def claude_hook_remove(config: ProjectConfig, payload: dict[str, Any]) -> int:
    """Remove a worktree this hook created: stop its stack, keep its volumes, tidy its branch."""
    raw = str(payload.get("worktree_path") or "").strip()
    if not raw:
        print("dk worktree claude-hook remove: hook input has no worktree_path", file=sys.stderr)
        return 1
    main_root = resolve_parent_repo_root(config)
    path = Path(raw).resolve()
    if path.parent != worktrees_dir(main_root).resolve():
        print(
            f"dk worktree claude-hook remove: {path} is not under "
            f"{worktrees_dir(main_root)}; leaving it alone",
            file=sys.stderr,
        )
        return 1

    try:
        overlay = load_worktree_overlay(path)
    except WorktreeOverlayError:
        overlay = None
    rc = worktree_remove(config, slug=path.name, yes=True)
    if rc != 0:
        return rc

    branch = overlay.branch if overlay else None
    if branch:
        deleted = _git("branch", "-d", branch, cwd=main_root, check=False, capture=True)
        if deleted.returncode != 0:
            print(
                f"Kept branch {branch}: it has commits not merged or pushed. "
                f"Delete it with: git branch -D {branch}",
                file=sys.stderr,
            )
    return 0


def cmd_claude_hook(event: str, config: ProjectConfig, stream: IO[str] | None = None) -> int:
    try:
        payload = read_hook_payload(stream or sys.stdin)
    except ValueError as exc:
        print(f"dk worktree claude-hook {event}: {exc}", file=sys.stderr)
        return 1
    if event == "create":
        return claude_hook_create(config, payload)
    return claude_hook_remove(config, payload)
