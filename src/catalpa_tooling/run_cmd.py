"""Echo subprocess argv before run (default) for CLI transparency."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Sequence
from typing import Any


def format_shell_command(cmd: Sequence[str]) -> str:
    """Shell-quote argv for display (POSIX)."""
    return shlex.join(list(cmd))


def run(cmd: Sequence[str], *, print_cmd: bool = True, **kwargs: Any) -> subprocess.CompletedProcess:
    if print_cmd:
        print(f"$ {format_shell_command(cmd)}", flush=True)
    return subprocess.run(cmd, **kwargs)
