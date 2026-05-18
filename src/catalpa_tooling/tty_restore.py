"""Restore shell TTY after Docker subprocesses (avoids broken Enter / ^M after Ctrl-C)."""

from __future__ import annotations

import subprocess
import sys


def restore_controlling_tty() -> None:
    """Run ``stty sane`` on the controlling terminal when stdin is a TTY.

    Docker and nested PTYs can leave the line discipline misconfigured; this resets
    typical cooked mode (e.g. CR→NL). Ignores failures (CI, non-tty).
    """
    if not sys.stdin.isatty():
        return
    try:
        subprocess.run(
            ["stty", "sane"],
            stdin=sys.stdin,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass
