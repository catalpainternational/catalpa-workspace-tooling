"""SELinux handling for dk's own ``docker run`` bind mounts (#128).

With SELinux enforcing (Fedora, RHEL and derivatives; Docker or Podman alike), a container
cannot read or write a host path that was bind-mounted without a label. Fixing that with
``:z`` relabels the host path for good, which is wrong for the paths dk mounts: a Caddyfile
inside a venv (lost on the next ``uv sync``), a user-chosen extract folder, the Docker socket.
dk's helpers instead run with ``--security-opt label=disable``, which leaves host labels alone
and only drops SELinux confinement for that one container.

The flag is added only when the engine reports SELinux, so on macOS and non-SELinux Linux
the commands are unchanged. ``DK_SELINUX_LABEL_DISABLE=1`` / ``=0`` overrides the detection.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from catalpa_tooling.run_cmd import run as run_cmd

SELINUX_LABEL_DISABLE_ENV = "DK_SELINUX_LABEL_DISABLE"
LABEL_DISABLE_ARGS: tuple[str, ...] = ("--security-opt", "label=disable")

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

# Keyed on the engine the CLI would talk to, so a remote DOCKER_HOST is asked separately.
_engine_cache: dict[tuple[str, str], bool] = {}


def _engine_key(env: Mapping[str, str]) -> tuple[str, str]:
    return (env.get("DOCKER_HOST") or "", env.get("DOCKER_CONTEXT") or "")


def engine_reports_selinux(env: Mapping[str, str] | None = None) -> bool:
    """True when ``docker info`` lists ``name=selinux`` among the engine's security options.

    Docker and Podman's compat API both report it when SELinux is enabled on the engine host.
    Any failure to ask counts as "no", so a broken ``docker info`` never changes a command.
    """
    run_env = dict(os.environ if env is None else env)
    key = _engine_key(run_env)
    if key in _engine_cache:
        return _engine_cache[key]
    result = run_cmd(
        ["docker", "info", "--format", "{{json .SecurityOptions}}"],
        env=run_env,
        check=False,
        print_cmd=False,
        capture_output=True,
        text=True,
    )
    found = result.returncode == 0 and "name=selinux" in (result.stdout or "")
    _engine_cache[key] = found
    return found


def selinux_label_disable_args(env: Mapping[str, str] | None = None) -> list[str]:
    """``--security-opt label=disable`` for a ``docker run`` that bind-mounts host paths, or ``[]``."""
    run_env = os.environ if env is None else env
    override = (run_env.get(SELINUX_LABEL_DISABLE_ENV) or "").strip().lower()
    if override in _TRUE:
        return list(LABEL_DISABLE_ARGS)
    if override in _FALSE:
        return []
    if override:
        print(
            f"Ignoring {SELINUX_LABEL_DISABLE_ENV}={override!r} (expected 1 or 0); detecting instead.",
            file=sys.stderr,
        )
    return list(LABEL_DISABLE_ARGS) if engine_reports_selinux(run_env) else []
