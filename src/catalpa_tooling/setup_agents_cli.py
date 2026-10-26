"""Top-level CLI: install agent guardrails and skills for Catalpa tooling repos."""

from __future__ import annotations

import argparse
import sys

from catalpa_tooling.agents_setup import (
    CLAUDE_SETTINGS,
    OPTIONAL_RULES,
    AgentsSetupPlan,
    SKILLS,
    apply_remove,
    apply_setup,
    inspect_status,
    plan_remove,
    plan_setup,
)
from catalpa_tooling.cli_interrupt import run_cli


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup-agents",
        description=(
            "Install agent guardrails (.cursorignore and .cursor/rules for Cursor; "
            ".claude/rules and Read deny rules in .claude/settings.json for Claude Code) and "
            "skills (.claude/skills, linked from .cursor/skills) from this tooling version. "
            f"Optional rules ({', '.join(OPTIONAL_RULES)}) and skills ({', '.join(SKILLS)}) "
            "are chosen under agents: in tooling.yaml. Run via `uv run setup-agents` "
            "from a tooling repo root."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned changes without writing files.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Report whether agent files are present, managed and current.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Also replace files that exist without the managed marker.",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove setup-agents-managed files only.",
    )
    return parser


def _print_status() -> int:
    status = inspect_status()
    print(f"repo: {status.repo_root}")
    for e in status.entries:
        rel = e.path.relative_to(status.repo_root)
        if e.owned:
            print(f"{rel}: owned by the project (no managed marker; left alone)")
            continue
        print(f"{rel}: present={e.present} managed={e.managed} current={e.current}")
    print(f"ready: {status.ready}")
    return 0 if status.ready else 1


def _print_skipped(plan: AgentsSetupPlan) -> None:
    for path in plan.skipped:
        rel = path.relative_to(plan.repo_root)
        if rel == CLAUDE_SETTINGS:
            reason = "it is not a JSON object with a permissions.deny list to merge into"
        else:
            reason = "the project owns it (no managed marker; --force replaces it)"
        print(f"setup-agents: left {rel} alone: {reason}.", file=sys.stderr)


def _setup_main() -> None:
    args = _build_parser().parse_args()

    if args.status:
        sys.exit(_print_status())

    if args.remove:
        plan = plan_remove()
        if plan.empty:
            print("setup-agents: nothing to remove.")
            return
        apply_remove(plan, dry_run=args.dry_run)
        if args.dry_run:
            print("setup-agents: dry run complete (no files changed).")
            return
        print("setup-agents: removed.")
        return

    plan = plan_setup(force=args.force)
    _print_skipped(plan)
    if plan.empty:
        print("setup-agents: already up to date (no changes needed).")
        return

    apply_setup(plan, dry_run=args.dry_run)
    if args.dry_run:
        print("setup-agents: dry run complete (no files written).")
        return
    print("setup-agents: installed. Commit these files so every contributor's agent gets them.")


def main() -> None:
    run_cli(_setup_main, label="setup-agents")
