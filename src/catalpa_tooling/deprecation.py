"""Deprecation warnings for renamed CLI commands and config keys."""

from __future__ import annotations

import sys


def warn_deprecated(old: str, new: str, *, context: str = "") -> None:
    """Print a one-line deprecation notice to stderr."""
    msg = f"Deprecated: use `{new}` instead of `{old}`."
    if context:
        msg = f"{msg} ({context})"
    print(msg, file=sys.stderr)
