"""Quiet ``KeyboardInterrupt``/config-error handling for console entrypoints (``native``, ``dk``, …)."""

from __future__ import annotations

import sys
from collections.abc import Callable

from catalpa_tooling.config import ProjectConfigError


def run_cli(main_fn: Callable[[], None], *, label: str) -> None:
    """Run ``main_fn()``; exit cleanly (no traceback) on Ctrl+C or a bad ``tooling.yaml``."""
    try:
        main_fn()
    except KeyboardInterrupt:
        print(f"\n{label}: interrupted.", file=sys.stderr)
        raise SystemExit(130) from None
    except ProjectConfigError as exc:
        print(f"{label}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
