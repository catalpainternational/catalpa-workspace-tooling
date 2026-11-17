# Working in this repository (agents)

Several agents often work on different issues in this repo at the same time. To keep them out of
each other's way, and out of the maintainer's way, **each task gets its own git worktree under
`.worktrees/`**.

## The main checkout belongs to the human

The main checkout is the repo root, not anything under `.worktrees/`. Don't edit files, commit,
switch branches, stash, rebase or reset there. Its branch and working tree may hold the
maintainer's in-progress work.

**The only exception** is a small change that touches **docs only** (`*.md`, `docs/`) and
nothing else: a typo, a README line, a changelog entry. That can go in the main checkout, on its
current branch, when the user asks. Anything touching code, tests, `pyproject.toml`, scripts,
CI, or agent/editor config (`.claude/`, `.cursor/`, `AGENTS.md`) goes in a worktree. If you're
unsure, use a worktree.

## Starting a task

Create the worktree **before your first edit**.

- **Claude Code:** use EnterWorktree, or start with `claude --worktree <name>`. This repo's
  `WorktreeCreate` hook (`.claude/hooks/worktree.py`) puts the worktree at `.worktrees/<name>` on a
  new branch `worktree/<name>`, cut from a freshly fetched `origin/main`. Subagents with
  `isolation: worktree` get one the same way.
- **Anything else (Cursor, Codex, a shell):** from the main checkout, run:

  ```bash
  git fetch origin main
  git worktree add --no-track -b fix/123-short-slug .worktrees/123-short-slug origin/main
  ```

Rules:

- Worktrees live in `.worktrees/` (gitignored). Not next to the repo (`../catalpa-workspace-tooling-*`),
  not in `.claude/worktrees/`, not in `/tmp`.
- Cut from `origin/main`, not the main checkout's `HEAD`, which may be stale or on an unrelated
  branch.
- Don't nest worktrees: create them from the main checkout, not from inside another worktree.
- One issue per worktree, one worktree per agent. Don't work in a worktree another agent is using.
  `git worktree list` shows them all.

## Working in it

- Give the branch a real name before pushing. The convention is `fix/<issue>-<slug>`,
  `feat/<issue>-<slug>`, `docs/<slug>` or `chore/<slug>`. For a hook-made worktree, rename it with
  `git branch -m worktree/<name> fix/123-short-slug`.
- Run every command from inside the worktree. Each worktree gets its own `.venv` on first
  `uv sync`:

  ```bash
  uv sync --group test
  uv run pytest
  ```

- Don't use bare `git stash` or `git stash pop`. The stash is shared by every worktree. Make a WIP
  commit instead.

## Finishing

1. Commit, push the branch, and open the PR from inside the worktree.
2. Remove the worktree once the PR is merged or the work is abandoned:
   - **Claude Code:** ExitWorktree with `action: remove` (add `discard_changes: true` once
     everything is pushed). The hook also deletes the local branch if `git branch -d` allows it.
   - **Otherwise:** `git worktree remove .worktrees/<name>` from the main checkout.
3. Subagent worktrees aren't removed automatically. Clean them up when their work is merged.
