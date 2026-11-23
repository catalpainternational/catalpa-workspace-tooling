"""Reuse one SSH connection per host and login for dk's many ``ssh`` calls.

A rendered host's commands (``host provision``, ``restore``, a bare ``dk <env>``) run a dozen or
more short ``ssh`` calls, each paying the TCP and key exchange again: about 3.4 s each from Dili
to sgp1, 0.7 s over a reused connection. OpenSSH's connection sharing keeps one open:
``ControlMaster=auto`` starts it on the first call, the others go through its socket, and
``ControlPersist`` keeps it for a minute after the last, so the next dk command reuses it too.

The sockets are in ``~/.ssh/dk-control/`` (0700; ``%C`` names one per user, host and port).
``DK_SSH_REUSE=0`` turns it off.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV = "DK_SSH_REUSE"
PERSIST_SECONDS = 60


def control_dir() -> Path | None:
    """The sockets' directory, made 0700; None when reuse is off or it can't be made."""
    if os.environ.get(ENV, "").strip().lower() in ("0", "false", "no", "off") or os.name == "nt":
        return None
    path = Path.home() / ".ssh" / "dk-control"
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    except OSError:
        return None
    return path


def reuse_options() -> list[str]:
    """``ssh`` options that share one connection per user, host and port (none when off)."""
    d = control_dir()
    if d is None:
        return []
    return [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={d}/%C",
        "-o", f"ControlPersist={PERSIST_SECONDS}",
    ]  # fmt: skip
