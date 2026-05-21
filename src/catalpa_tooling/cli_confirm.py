"""Interactive confirmations for ``dk`` destructive actions (type environment name)."""

from __future__ import annotations

import sys


def confirm_by_typing_expected(expected: str, *, hint: str | None = None) -> bool:
    """Print prompt on stderr; return True iff the user typed ``expected`` exactly."""
    print(file=sys.stderr)
    if hint:
        prompt = f"Type {expected!r} ({hint}) to confirm, or press Enter to cancel: "
    else:
        prompt = f"Type {expected!r} exactly to confirm, or press Enter to cancel: "
    print(prompt, file=sys.stderr, end="")
    try:
        line = input()
    except EOFError:
        return False
    return line.strip() == expected


def confirm_by_typing_env_name(env_name: str) -> bool:
    """Print prompt on stderr; read answer from stdin.

    Returns True iff the user typed ``env_name`` exactly (after stripping leading/trailing
    whitespace on the line). EOF cancels.
    """
    return confirm_by_typing_expected(env_name, hint="environment name")


def confirm_yes_default_no(prompt: str) -> bool:
    """Print ``prompt`` on stderr; return True only if user answers y/yes (case-insensitive)."""
    print(prompt, file=sys.stderr, end="")
    try:
        line = input()
    except EOFError:
        return False
    return line.strip().lower() in ("y", "yes")
