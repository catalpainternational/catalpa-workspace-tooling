"""Quiet ``KeyboardInterrupt`` handling for console entrypoints (``uv run native``, ``dk``, …)."""

from __future__ import annotations

import sys
from collections.abc import Callable


def run_cli(main_fn: Callable[[], None], *, label: str) -> None:
    """Run ``main_fn()``; on Ctrl+C print one line and exit 130 (no traceback)."""
    try:
        main_fn()
    except KeyboardInterrupt:
        print(f"\n{label}: interrupted.", file=sys.stderr)
        raise SystemExit(130) from None
