"""Resolve and invoke the host ``s3cmd`` binary for DigitalOcean Spaces bucket operations."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from catalpa_tooling.run_cmd import run as run_cmd

INSTALL_HINT = "Install s3cmd: https://s3tools.org/releasenotes"


class S3cmdNotFoundError(RuntimeError):
    """System ``s3cmd`` binary is missing or not executable."""


class S3cmdCommandError(RuntimeError):
    """Host ``s3cmd`` exited with a non-zero status."""

    def __init__(self, message: str, *, returncode: int) -> None:
        super().__init__(message)
        self.returncode = returncode


def print_s3cmd_required(err: S3cmdNotFoundError | None = None) -> None:
    """Print a short hint when host ``s3cmd`` is required but not installed."""
    if err is not None and str(err).strip():
        print(str(err), file=sys.stderr)
    print(INSTALL_HINT, file=sys.stderr)


def resolve_s3cmd_binary() -> Path:
    """Return path to the ``s3cmd`` executable."""
    override = os.environ.get("S3CMD_BIN", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return p.resolve()
        raise S3cmdNotFoundError(f"S3CMD_BIN is not executable: {p}")

    found = shutil.which("s3cmd")
    if found:
        p = Path(found)
        if p.is_file() and os.access(p, os.X_OK):
            return p.resolve()

    raise S3cmdNotFoundError(INSTALL_HINT)


def ensure_s3cmd_available() -> Path:
    """Return host ``s3cmd`` path, or raise ``S3cmdNotFoundError``."""
    return resolve_s3cmd_binary()


def run_s3cmd(
    args: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = False,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    binary = ensure_s3cmd_available()
    cmd = [str(binary), *args]
    kwargs: dict = {
        "check": check,
        "text": True,
        "env": env,
    }
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    return run_cmd(cmd, **kwargs)
