"""Top-level CLI: one-time zsh + direnv bootstrap for Catalpa tooling repos."""

from __future__ import annotations

import argparse
import sys

from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.shell_setup import (
    apply_setup,
    apply_remove,
    build_next_steps,
    inspect_status,
    next_steps_context,
    plan_remove,
    plan_setup,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup-shell",
        description=(
            "One-time machine setup: install catalpa-direnv.zsh and patch ~/.zshrc "
            "with the direnv hook (and optional tab-completion source). "
            "Run via `uv run setup-shell` from inside a tooling repo after `direnv allow`."
        ),
    )
    parser.add_argument(
        "--zshrc",
        type=str,
        default=None,
        metavar="PATH",
        help="Zsh rc file to patch (default: ~/.zshrc or $ZDOTDIR/.zshrc).",
    )
    parser.add_argument(
        "--skip-direnv-hook",
        action="store_true",
        help="Do not add eval \"$(direnv hook zsh)\" to ~/.zshrc.",
    )
    parser.add_argument(
        "--skip-completion",
        action="store_true",
        help="Do not install ~/.config/catalpa/direnv.zsh or source it from ~/.zshrc.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned changes without writing files.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Report current shell setup and exit.",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove catalpa-shell-setup from ~/.zshrc and ~/.config/catalpa/direnv.zsh.",
    )
    return parser


def _print_status() -> int:
    status = inspect_status()
    print(f"zshrc: {status.zshrc_path}")
    print(f"  exists: {status.zshrc_exists}")
    print(f"  catalpa block: {status.catalpa_block_present}")
    print(f"  direnv hook: {status.direnv_hook_present}")
    print(f"  catalpa source: {status.catalpa_source_present}")
    print(f"catalpa-direnv.zsh installed: {status.catalpa_direnv_installed}")
    print(f"  matches package: {status.catalpa_direnv_matches_package}")
    if status.legacy_dk_wrapper or status.legacy_argcomplete_eval:
        print("legacy config detected in ~/.zshrc (see warnings when running install)")
    ready = (
        status.direnv_hook_present
        and status.catalpa_direnv_installed
        and status.catalpa_direnv_matches_package
        and status.catalpa_source_present
    )
    print(f"ready: {ready}")
    return 0 if ready else 1


def _print_next_steps(*, zshrc_path, zshrc_changed: bool) -> None:
    ctx = next_steps_context(zshrc_path=zshrc_path, zshrc_changed=zshrc_changed)
    steps = build_next_steps(ctx)
    print()
    if steps:
        print("Run in your shell (setup-shell cannot reload the parent shell for you):")
        for step in steps:
            print(f"  {step}")
    elif ctx.dk_on_path:
        print("Shell setup is active in this directory (dk is on PATH).")
    else:
        print("Shell setup files are in place; open a new shell if dk is not on PATH yet.")


def _print_reload_hint(*, zshrc_path) -> None:
    print()
    print("Run in your shell (setup-shell cannot reload the parent shell for you):")
    print(f"  source {zshrc_path}")


def _setup_main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.status:
        sys.exit(_print_status())

    from pathlib import Path

    zshrc_path = Path(args.zshrc).expanduser() if args.zshrc else None

    if args.remove:
        plan = plan_remove(zshrc_path=zshrc_path)
        if not plan.remove_zshrc_block and not plan.remove_catalpa_direnv:
            print("setup-shell: nothing to remove.")
            return
        apply_remove(plan, dry_run=args.dry_run)
        if args.dry_run:
            print("setup-shell: dry run complete (no files changed).")
            return
        print("setup-shell: removed.")
        _print_reload_hint(zshrc_path=plan.zshrc_path)
        return

    plan = plan_setup(
        zshrc_path=zshrc_path,
        skip_direnv_hook=args.skip_direnv_hook,
        skip_completion=args.skip_completion,
    )
    resolved_zshrc = plan.zshrc_path

    for warning in plan.warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if not plan.write_catalpa_direnv and not plan.patch_zshrc:
        print("setup-shell: already configured (no changes needed).")
        _print_next_steps(zshrc_path=resolved_zshrc, zshrc_changed=False)
        return

    apply_setup(plan, dry_run=args.dry_run)

    if args.dry_run:
        print("setup-shell: dry run complete (no files written).")
        return

    print("setup-shell: installed.")
    _print_next_steps(zshrc_path=resolved_zshrc, zshrc_changed=plan.patch_zshrc)


def main() -> None:
    run_cli(_setup_main, label="setup-shell")
