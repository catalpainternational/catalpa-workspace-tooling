"""Low-level docker compose invocation for the dk CLI."""

from __future__ import annotations

import os
import subprocess
import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.tty_restore import restore_controlling_tty

if TYPE_CHECKING:
    from catalpa_tooling.config import ProjectConfig


def _compose(
    compose_file: str,
    *args: str,
    check: bool = True,
    env_add: dict[str, str] | None = None,
    extra_compose_files: list[str] | None = None,
    print_cmd: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess:
    """Run docker compose with the given file and args. env_add is merged into the process env."""
    cmd = ["docker", "compose", "-f", compose_file]
    if extra_compose_files:
        for f in extra_compose_files:
            cmd.extend(["-f", f])
    cmd.extend(list(args))
    run_env = os.environ.copy()
    if env_add:
        run_env.update(env_add)
    run_kwargs: dict[str, object] = {}
    if capture_output:
        run_kwargs["capture_output"] = True
        run_kwargs["text"] = True
    try:
        return run_cmd(
            cmd,
            check=check,
            env=run_env,
            print_cmd=print_cmd,
            **run_kwargs,
        )
    finally:
        restore_controlling_tty()


def _healthcheck_host_header(env_add: dict[str, str] | None) -> str | None:
    """Hostname for in-container HTTP probes (matches Django ALLOWED_HOSTS from BERO_ORIGIN)."""
    if not env_add:
        return None
    for key in ("BERO_ORIGIN", "SITE_ORIGIN", "DJANGO_ORIGIN"):
        raw = (env_add.get(key) or "").strip()
        if not raw:
            continue
        if "://" not in raw:
            raw = f"http://{raw}"
        host = urlparse(raw).hostname
        if host:
            return host
    return None


def _healthcheck_python_snippet(url: str, *, host_header: str | None = None) -> str:
    if host_header:
        return (
            "import sys, urllib.request\n"
            "try:\n"
            f"    req = urllib.request.Request({url!r}, headers={{'Host': {host_header!r}}})\n"
            "    urllib.request.urlopen(req, timeout=2)\n"
            "except Exception:\n"
            "    sys.exit(1)\n"
        )
    return (
        "import sys, urllib.request\n"
        "try:\n"
        f"    urllib.request.urlopen({url!r}, timeout=2)\n"
        "except Exception:\n"
        "    sys.exit(1)\n"
    )


def _is_web_service_healthy(
    compose_file: str,
    config: ProjectConfig,
    *,
    env_add: dict[str, str] | None = None,
) -> bool:
    """Return True if the configured web service healthcheck URL responds."""
    hc = config.stack.healthcheck
    host_header = _healthcheck_host_header(env_add)
    r = _compose(
        compose_file,
        "exec",
        "-T",
        hc.service,
        "python",
        "-c",
        _healthcheck_python_snippet(hc.url, host_header=host_header),
        env_add=env_add,
        check=False,
        print_cmd=False,
    )
    return r.returncode == 0


def _wait_for_web_service(
    compose_file: str,
    config: ProjectConfig,
    *,
    env_add: dict[str, str] | None = None,
    timeout_seconds: int = 120,
    poll_interval: int = 3,
) -> bool:
    """Wait until the web service is healthy or timeout. Returns True if healthy."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _is_web_service_healthy(compose_file, config, env_add=env_add):
            return True
        time.sleep(poll_interval)
    return False
