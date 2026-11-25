"""One interface over an env's stack, whatever runs it (M5).

A ``runtime: compose`` env runs its stack with ``docker compose`` (local, or on a host over
``DOCKER_HOST``); a ``runtime: quadlet`` env runs it as rendered Podman quadlets
(``fcos/backend.py``). Data operations (fetch, restore, transfer, hooks) were written twice,
once per runtime. ``StackBackend`` is the set of primitives they need, so each can be written
once over it.

The protocol grows as operations move onto it. This first step adds the interface and
``ComposeBackend``, which only delegates to the functions compose callers use today; no call
site uses it yet, so nothing changes. Behaviour that really differs between the runtimes
(pgBackRest restore, restic restore, backup units) stays per backend.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.managed_deploy_env import ManagedDeployContext

Runtime = Literal["compose", "quadlet"]


@runtime_checkable
class StackBackend(Protocol):
    config: ProjectConfig
    env_name: str

    @property
    def runtime(self) -> Runtime: ...

    def describe(self) -> str:
        """Where the stack runs, for messages (``compose on ssh://…``)."""
        ...

    def ensure_db_running(self) -> int:
        """Start the db service if it isn't running; 0 once it answers."""
        ...

    def stop_writers(self) -> object:
        """Stop what writes to the db and media (db keeps running). Returns a token: pass it to
        ``start_writers`` whatever its value."""
        ...

    def start_writers(self, token: object) -> int:
        """Start again what ``stop_writers`` stopped."""
        ...

    def manage(self, manage_args: list[str], *, user: str | None, tty: bool) -> int:
        """``./manage.py …`` in the web service, as ``stack.manage_user`` unless ``user``."""
        ...

    def pull_media(self, target: Path) -> int:
        """Copy the env's media to the local directory ``target``."""
        ...


@dataclass
class ComposeBackend:
    """A ``runtime: compose`` env, over its resolved deploy context."""

    ctx: ManagedDeployContext

    runtime: Runtime = "compose"

    @property
    def config(self) -> ProjectConfig:
        return self.ctx.config

    @property
    def env_name(self) -> str:
        return self.ctx.env_name

    def describe(self) -> str:
        return f"{self.env_name} (compose on {self.ctx.docker_host or 'the local Docker'})"

    def ensure_db_running(self) -> int:
        from catalpa_tooling import pgbackrest_db

        return pgbackrest_db.ensure_db_service_running(
            self.ctx.compose_file, self.ctx.env_add, config=self.config, dk_env_name=self.env_name
        )

    def stop_writers(self) -> object:
        from catalpa_tooling import dk_transfer

        dk_transfer._stop_dest_writers(self.ctx.compose_file, self.ctx.env_add, dry_run=False)
        return None  # start_writers brings the whole stack up: nothing to remember

    def start_writers(self, token: object) -> int:
        """``docker compose up -d`` with dk's overrides. Problems are warnings, as in transfer
        today, so this returns 0."""
        from catalpa_tooling import dk_transfer

        dk_transfer._start_dest_writers(
            self.ctx.compose_file,
            self.ctx.env_add,
            dry_run=False,
            config=self.config,
            env_name=self.env_name,
            info=self.ctx.info,
        )
        return 0

    def manage(self, manage_args: list[str], *, user: str | None, tty: bool) -> int:
        from catalpa_tooling import compose

        return compose._compose(
            self.ctx.compose_file,
            *compose.manage_exec_args(self.config, manage_args, tty=tty, user=user),
            env_add=self.ctx.env_add,
            check=False,
        ).returncode

    def pull_media(self, target: Path, *, dry_run: bool = False, alpine_image: str = "alpine:3.21") -> int:
        from catalpa_tooling import media_pull

        return media_pull.run_pull_media(
            self.ctx.env_add,
            target=target,
            dry_run=dry_run,
            alpine_image=alpine_image,
            config=self.config,
            compose_file=self.ctx.compose_file,
        )


class BackendError(Exception):
    pass


def backend_for(config: ProjectConfig, env_name: str) -> StackBackend:
    """The env's backend: the rendered quadlet host for ``runtime: quadlet``, else compose.

    The compose context decrypts the env's credentials (SOPS); a quadlet env reads only its
    ``docker_host`` and committed render. Raises ``BackendError`` when either can't be loaded.
    """
    from catalpa_tooling.fcos.backend import EnvError, quadlet_backend
    from catalpa_tooling.managed_deploy_env import load_managed_deploy_context

    try:
        quadlet = quadlet_backend(config, env_name)
    except EnvError as exc:
        raise BackendError(f"{env_name}: {exc}") from None
    if quadlet is not None:
        return quadlet
    ctx = load_managed_deploy_context(config, env_name)
    if ctx is None:  # it has printed why
        print(f"{env_name}: couldn't load the env's deploy settings", file=sys.stderr)
        raise BackendError(f"{env_name}: no deploy context")
    return ComposeBackend(ctx)
