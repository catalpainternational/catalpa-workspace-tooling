"""Top-level CLI: scaffold VS Code tasks for Catalpa tooling repos."""

from __future__ import annotations

import argparse
import sys

from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.vscode_setup import (
    WorkflowOverride,
    apply_remove,
    apply_setup,
    inspect_status,
    plan_remove,
    plan_setup,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup-vscode",
        description=(
            "Scaffold VS Code tasks for local Docker development (dk dev and dk full). "
            "Run via `uv run setup-vscode` from a tooling repo root."
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
        help="Report whether setup-vscode files are present and current.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite managed .vscode files even if already current.",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove setup-vscode-managed .vscode files only.",
    )
    parser.add_argument(
        "--workflow",
        choices=("auto", "docker"),
        default="auto",
        help="Task workflow to generate (default: auto — dk dev, plus dk full when configured).",
    )
    return parser


def _print_status(workflow_override: WorkflowOverride) -> int:
    status = inspect_status(workflow_override=workflow_override)
    print(f"repo: {status.repo_root}")
    print(f"workflow: {status.workflow.value}")
    print(f"tasks.json: present={status.tasks_present} managed={status.tasks_managed} current={status.tasks_current}")
    print(
        "extensions.json: "
        f"present={status.extensions_present} managed={status.extensions_managed} "
        f"current={status.extensions_current}"
    )
    print(
        "settings.json: "
        f"present={status.settings_present} managed={status.settings_managed} "
        f"current={status.settings_current}"
    )
    print(f".gitignore vscode exceptions: {status.gitignore_patched}")
    print(f"ready: {status.ready}")
    return 0 if status.ready else 1


def _setup_main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    workflow: WorkflowOverride = args.workflow

    if args.status:
        sys.exit(_print_status(workflow))

    if args.remove:
        plan = plan_remove()
        if not (plan.remove_tasks or plan.remove_extensions or plan.remove_settings):
            print("setup-vscode: nothing to remove.")
            return
        apply_remove(plan, dry_run=args.dry_run)
        if args.dry_run:
            print("setup-vscode: dry run complete (no files changed).")
            return
        print("setup-vscode: removed.")
        return

    plan = plan_setup(workflow_override=workflow, force=args.force)
    if not (
        plan.write_tasks
        or plan.write_extensions
        or plan.write_settings
        or plan.patch_gitignore
    ):
        print("setup-vscode: already configured (no changes needed).")
        print(f"workflow: {plan.workflow.value}")
        print("Run tasks via Terminal → Run Task (Cmd+Shift+P → “Tasks: Run Task”).")
        return

    print(f"workflow: {plan.workflow.value}")
    apply_setup(plan, dry_run=args.dry_run)

    if args.dry_run:
        print("setup-vscode: dry run complete (no files written).")
        return

    print("setup-vscode: installed.")
    print("Run tasks via Terminal → Run Task (Cmd+Shift+P → “Tasks: Run Task”).")
    print(
        "Common tasks: Dev: Start stack, Dev: Open site in browser, "
        "Dev: Open site in Cursor browser, Dev: Run Django command "
        "(and Full: … when docker/envs/full is configured)."
    )


def main() -> None:
    run_cli(_setup_main, label="setup-vscode")
