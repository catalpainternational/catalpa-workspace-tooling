"""Echo subprocess argv before run (default) for CLI transparency."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from typing import Any

# Conventional exit status when the user interrupts (128 + SIGINT).
_EXIT_SIGINT = 130


def format_shell_command(cmd: Sequence[str]) -> str:
    """Shell-quote argv for display (POSIX)."""
    return shlex.join(list(cmd))


def terminate_process_tree(proc: subprocess.Popen[Any], *, timeout: float = 30) -> None:
    """Send SIGTERM (then SIGKILL) to ``proc`` and its process group (POSIX)."""
    if proc.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                return
        else:
            proc.kill()
        proc.wait()


def run_interruptible(
    cmd: Sequence[str],
    *,
    print_cmd: bool = True,
    on_interrupt: Callable[[], None] | None = None,
    terminate_timeout: float = 30,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Run ``cmd`` and stop the full process tree on Ctrl-C.

    Unlike ``subprocess.run``, Ctrl-C terminates the child (and its process group on
    POSIX) instead of leaving long-running grandchildren such as ``docker compose run``
    containers behind. A SIGINT handler runs cleanup immediately — needed because
    ``docker compose run`` often stops streaming logs on Ctrl-C while the CLI keeps
    blocking in ``wait()`` until the container exits, without raising ``KeyboardInterrupt``
    in the parent. Optional ``on_interrupt`` runs after the tree is signalled (e.g.
    stop/remove orphaned one-off containers).
    """
    if print_cmd:
        print(f"$ {format_shell_command(cmd)}", flush=True)
    popen_kwargs = dict(kwargs)
    popen_kwargs.pop("check", None)
    proc = subprocess.Popen(list(cmd), start_new_session=True, **popen_kwargs)
    interrupted = threading.Event()
    old_sigint = signal.getsignal(signal.SIGINT)

    def _handle_sigint(signum: int, frame: object | None) -> None:
        if interrupted.is_set():
            if callable(old_sigint) and old_sigint not in (
                signal.SIG_DFL,
                signal.SIG_IGN,
            ):
                old_sigint(signum, frame)  # type: ignore[misc, operator]
            raise KeyboardInterrupt
        interrupted.set()
        print("\nInterrupted.", file=sys.stderr, flush=True)
        terminate_process_tree(proc, timeout=terminate_timeout)
        if on_interrupt is not None:
            on_interrupt()

    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        try:
            rc = proc.wait()
        except KeyboardInterrupt:
            if not interrupted.is_set():
                interrupted.set()
                print("\nInterrupted.", file=sys.stderr, flush=True)
                terminate_process_tree(proc, timeout=terminate_timeout)
                if on_interrupt is not None:
                    on_interrupt()
            return subprocess.CompletedProcess(list(cmd), _EXIT_SIGINT)
        if interrupted.is_set():
            return subprocess.CompletedProcess(list(cmd), _EXIT_SIGINT)
        return subprocess.CompletedProcess(list(cmd), rc)
    finally:
        signal.signal(signal.SIGINT, old_sigint)


def run(cmd: Sequence[str], *, print_cmd: bool = True, **kwargs: Any) -> subprocess.CompletedProcess:
    if print_cmd:
        print(f"$ {format_shell_command(cmd)}", flush=True)
    return subprocess.run(cmd, **kwargs)
