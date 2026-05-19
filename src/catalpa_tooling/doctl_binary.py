"""Resolve and invoke the host DigitalOcean ``doctl`` binary (not this package's console script)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from catalpa_tooling.run_cmd import run as run_cmd

INSTALL_HINT = (
    "Install the official DigitalOcean CLI: https://docs.digitalocean.com/reference/doctl/how-to/install/"
)


class DoctlNotFoundError(RuntimeError):
    """System ``doctl`` binary is missing or not executable."""


def _entrypoint_path() -> Path | None:
    raw = sys.argv[0] if sys.argv else ""
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except OSError:
        return None


def _is_catalpa_doctl_wrapper(path: Path) -> bool:
    """True if ``path`` is this package's ``doctl`` console script, not the official CLI."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if not resolved.is_file():
        return False
    try:
        if resolved.stat().st_size > 50_000:
            return False
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "catalpa_tooling.doctl_cli" in content


def _consider_doctl_candidate(
    candidate: Path,
    *,
    entry: Path | None,
    seen: set[Path],
    out: list[Path],
) -> None:
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return
    try:
        resolved = candidate.resolve()
    except OSError:
        return
    if resolved in seen:
        return
    seen.add(resolved)
    if _is_catalpa_doctl_wrapper(resolved):
        return
    if entry is not None and resolved == entry:
        return
    out.append(resolved)


def resolve_doctl_binary() -> Path:
    """Return path to the real ``doctl`` executable (skip this package's venv shim)."""
    override = os.environ.get("DOCTL_BIN", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return p.resolve()
        raise DoctlNotFoundError(f"DOCTL_BIN is not executable: {p}")

    entry = _entrypoint_path()
    seen: set[Path] = set()
    candidates: list[Path] = []

    found = shutil.which("doctl")
    if found:
        _consider_doctl_candidate(Path(found), entry=entry, seen=seen, out=candidates)

    for part in os.environ.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        _consider_doctl_candidate(Path(part) / "doctl", entry=entry, seen=seen, out=candidates)

    if candidates:
        return candidates[0]

    raise DoctlNotFoundError(INSTALL_HINT)


def ensure_doctl_available() -> Path:
    try:
        return resolve_doctl_binary()
    except DoctlNotFoundError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e


def build_doctl_argv(
    binary: Path | str,
    args: Sequence[str],
    *,
    context: str | None = None,
) -> list[str]:
    cmd = [str(binary), *args]
    if context:
        cmd.extend(["--context", context])
    return cmd


def run_doctl(
    args: Sequence[str],
    *,
    context: str | None = None,
    check: bool = False,
    capture_output: bool = False,
    stdin: int | None = None,
) -> subprocess.CompletedProcess[str]:
    binary = ensure_doctl_available()
    cmd = build_doctl_argv(binary, args, context=context)
    kwargs: dict = {
        "check": check,
        "text": True,
    }
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if stdin is not None:
        kwargs["stdin"] = stdin
    return run_cmd(cmd, **kwargs)


def format_doctl_failure(result: subprocess.CompletedProcess[str]) -> str:
    """Build a user-visible message when ``doctl`` fails (errors often land on stdout as JSON)."""
    err = (result.stderr or "").strip()
    if err:
        return err
    out = (result.stdout or "").strip()
    if out:
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            return out
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list):
                parts = [
                    str(item.get("detail") or item.get("message") or item)
                    for item in errors
                    if item
                ]
                if parts:
                    return "\n".join(parts)
            message = payload.get("message")
            if message:
                return str(message)
        return out
    return f"doctl exited {result.returncode}"


def run_doctl_json(
    args: Sequence[str],
    *,
    context: str | None = None,
) -> object:
    result = run_doctl([*args, "-o", "json"], context=context, capture_output=True)
    if result.returncode != 0:
        print(format_doctl_failure(result), file=sys.stderr)
        raise SystemExit(result.returncode)
    raw = (result.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON from doctl: {e}", file=sys.stderr)
        raise SystemExit(1) from e
    if isinstance(data, dict) and "errors" in data:
        print(format_doctl_failure(result), file=sys.stderr)
        raise SystemExit(1)
    return data
