"""One interface over an env's stack, whatever runs it (M5).

A ``runtime: compose`` env runs its stack with ``docker compose`` (local, or on a host over
``DOCKER_HOST``); a ``runtime: quadlet`` env runs it as rendered Podman quadlets
(``fcos/backend.py``). Data operations (fetch, restore, transfer, hooks) were written twice,
once per runtime. ``StackBackend`` is the set of primitives they need, so each can be written
once over it.

The protocol grows as operations move onto it; ``ComposeBackend`` delegates to the functions
compose callers already used. Moved so far: ``dk fetch db`` (``via: dk``), ``dk fetch media``
(and the deprecated ``dk <env> pull_media``), the post-restore hooks, restoring a dump
archive (``db_archive.restore_archive``), ``dk transfer`` (``stack_transfer``) and the
``dk <env> db`` / ``files`` / ``restore`` commands (``stack_commands``). Behaviour that really
differs between the runtimes (pgBackRest and restic themselves, backup units) stays per backend.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cached_property
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

    def dump_db(
        self, dest: Path, pg_dump_args: Sequence[str] = (), *, database: str | None = None, min_bytes: int = 0
    ) -> int:
        """``pg_dump -Fc`` of ``database`` to the local file ``dest``; None is the app database
        as the db container resolves it (``$APP_DB``). db must be running. Fails when the output
        isn't a ``-Fc`` archive or is smaller than ``min_bytes`` (``dk fetch db`` asks for a
        real-sized one; a transfer takes an empty database too), and leaves no partial ``dest``.
        Prints nothing on success: the caller says what it's doing."""
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

    def run_manage_once(self, manage_args: list[str], *, user: str | None = None) -> int:
        """``./manage.py …`` in a throwaway container of the web service (compose: ``run --rm
        --no-deps``; quadlet: the ``<web>-task`` unit). Needs db running, not web. A one-shot
        command, for work that must not depend on the app being up (post-restore hooks)."""
        ...

    def copy_into_db(self, archive: Path, container_path: str) -> int:
        """Put the local file ``archive`` at ``container_path`` inside the db container, readable
        by ``postgres`` (quadlet also checks its SHA-256 there). db must be running."""
        ...

    def db_sh(self, script: str, *, root: bool = False) -> int:
        """``sh -c script`` in the db container, as root when asked."""
        ...

    def run_post_restore_hooks(self, target: Literal["app", "metabase"] = "app") -> int:
        """The ``ops.post_db_restore`` (``target="metabase"``: ``post_metabase_db_restore``)
        hooks for this env, after a restore of that database. 0 when none apply."""
        ...

    def media_rsync_source(self) -> str | None:
        """Where rsync can read the env's media: ``user@host:/path`` or a local path. None when
        nothing can (compose on a local engine whose volumes the host can't read)."""
        ...

    def pull_media(self, target: Path, *, dry_run: bool = False) -> int:
        """Copy the env's media into the local directory ``target`` (``dk fetch media``):
        rsync from ``media_rsync_source``, adding and updating files, deleting none."""
        ...

    def push_media(self, source: Path, *, dry_run: bool = False) -> int:
        """Make the env's media match the local directory ``source`` (rsync ``--delete``)."""
        ...

    def describe_media(self) -> str:
        """Where the env's media lives, for messages. Call after ``preflight_transfer``."""
        ...

    def preflight_transfer(
        self, *, role: Literal["source", "destination"], db: bool, media: bool, dry_run: bool = False
    ) -> list[str]:
        """Before ``dk transfer`` changes anything: what would stop this end from taking part,
        as messages (empty: ready). ``db``/``media``: the legs that will run. Starts db (and,
        on compose, creates missing external volumes), except with ``dry_run``: that changes
        nothing, and prints what a real run would start or create instead."""
        ...

    # --- dk <env> db / files / restore (``stack_commands``) ---------------------------------

    #: Whether restoring a dump stops the app around it (quadlet does; compose never has).
    dump_restore_stops_app: bool

    def refused_subcommand(self, verb: str, sub: str) -> str | None:
        """Why ``dk <env> <verb> <sub>`` (verb: ``db``/``files``) isn't on this runtime, and what
        to use instead; None when it is."""
        ...

    def ensure_backup_credentials(
        self, target: Literal["pgbackrest", "restic"], *, yes: bool, dry_run: bool, label: str
    ) -> int:
        """Create the repository's WRITE credentials when missing (and put them where the
        stack reads them). 0 when they're in place."""
        ...

    def pgbackrest(self, sub: str, ns: argparse.Namespace, *, dry_run: bool) -> int:
        """``dk <env> db <sub>``, every subcommand but ``restore``."""
        ...

    def restic(self, sub: str, ns: argparse.Namespace, *, dry_run: bool) -> int:
        """``dk <env> files <sub>``, every subcommand but ``restore``."""
        ...

    def pgbackrest_restore_available(self) -> bool:
        """Whether ``db restore`` can restore from a pgBackRest repository (the env's own, or
        ``restore_from``'s with a read key)."""
        ...

    def pgbackrest_restore(self, extra: list[str], *, yes: bool, dry_run: bool) -> int:
        """pgBackRest restore of the whole cluster, confirmation and post-restore hooks
        included. ``extra``: pgBackRest restore options."""
        ...

    def files_restore(self, snapshot: str, *, yes: bool, dry_run: bool) -> int:
        """Replace the media with a restic snapshot, confirmation included."""
        ...

    def restore_all(self, extra: list[str], snapshot: str, *, yes: bool, dry_run: bool) -> int:
        """``dk <env> restore``: the database and the media, from the env's backups (or
        ``restore_from``'s), after one confirmation."""
        ...

    def restore_hooks_problem(self, *, metabase: bool) -> str | None:
        """Why the post-restore hooks (Metabase's too, with ``metabase``) couldn't run, checked
        before a restore replaces anything; None when they can."""
        ...


@dataclass
class ComposeBackend:
    """A ``runtime: compose`` env, over its resolved deploy context."""

    ctx: ManagedDeployContext

    runtime: Runtime = "compose"
    dump_restore_stops_app: bool = False
    _media_storage: object = field(default=None, init=False, repr=False, compare=False)

    @property
    def config(self) -> ProjectConfig:
        return self.ctx.config

    @property
    def env_name(self) -> str:
        return self.ctx.env_name

    @cached_property
    def env(self) -> dict[str, str]:
        """The context's env with ``COMPOSE_PROJECT_NAME`` set as ``dk <env>`` sets it (a local
        env's project is ``<default>_<env>``), so volumes resolve to the env's own."""
        from catalpa_tooling.restic_files import resolve_env_with_compose_project

        return resolve_env_with_compose_project(
            self.ctx.compose_file, self.ctx.env_add, config=self.config, dk_env_name=self.env_name
        )

    def describe(self) -> str:
        return f"{self.env_name} (compose on {self.ctx.docker_host or 'the local Docker'})"

    def ensure_db_running(self) -> int:
        from catalpa_tooling import pgbackrest_db

        return pgbackrest_db.ensure_db_service_running(
            self.ctx.compose_file, self.env, config=self.config, dk_env_name=self.env_name
        )

    def dump_db(
        self, dest: Path, pg_dump_args: Sequence[str] = (), *, database: str | None = None, min_bytes: int = 0
    ) -> int:
        from catalpa_tooling import pgbackrest_db
        from catalpa_tooling.pgbackrest_db import _PG_DUMP_CUSTOM_MAGIC

        rc = pgbackrest_db.run_pg_dump_to_file(
            self.ctx.compose_file, self.env, dest, list(pg_dump_args) or None, database=database
        )
        if rc != 0:
            return rc
        size = dest.stat().st_size
        with open(dest, "rb") as f:
            magic_ok = f.read(5) == _PG_DUMP_CUSTOM_MAGIC
        problem = (
            f"{dest} does not look like a pg_dump -Fc archive"
            if not magic_ok
            else f"dump at {dest} is only {size} bytes (expected at least {min_bytes})"
            if size < min_bytes
            else None
        )
        if problem:
            print(f"pg_dump ({self.env_name}): {problem}.", file=sys.stderr)
            dest.unlink(missing_ok=True)
            return 1
        return 0

    def stop_writers(self) -> object:
        from catalpa_tooling import dk_transfer

        dk_transfer._stop_dest_writers(self.ctx.compose_file, self.env, dry_run=False)
        return None  # start_writers brings the whole stack up: nothing to remember

    def start_writers(self, token: object) -> int:
        """``docker compose up -d`` with dk's overrides. Problems are warnings, as in transfer
        today, so this returns 0."""
        from catalpa_tooling import dk_transfer

        dk_transfer._start_dest_writers(
            self.ctx.compose_file,
            self.env,
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
            env_add=self.env,
            check=False,
        ).returncode

    def run_manage_once(self, manage_args: list[str], *, user: str | None = None) -> int:
        from catalpa_tooling import compose

        return compose._compose(
            self.ctx.compose_file,
            *compose.manage_run_args(self.config, manage_args, user=user),
            env_add=self.env,
            check=False,
        ).returncode

    @classmethod
    def from_parts(
        cls,
        config: ProjectConfig,
        env_name: str,
        compose_file: str,
        env_add: dict[str, str],
        *,
        docker_host: str = "",
        info: dict | None = None,
    ) -> ComposeBackend:
        """A backend from what a ``dk <env>`` handler already holds. The context fields only
        media and deploy use (origins, registry, storage volumes) are left empty: don't call
        the media methods on this one."""
        ctx = ManagedDeployContext(
            env_name=env_name,
            compose_file=compose_file,
            env_add=env_add,
            docker_host=docker_host,
            dc_backup_docker_host="",
            site_origin="",
            site_origins=(),
            use_prepulled_registry=False,
            image_registry="",
            info_tag=None,
            config=config,
            storage_volumes={},
            info=info or {},
        )
        return cls(ctx)

    def copy_into_db(self, archive: Path, container_path: str) -> int:
        from catalpa_tooling import pgbackrest_db

        return pgbackrest_db.copy_archive_into_db(self.ctx.compose_file, self.env, archive, container_path)

    def db_sh(self, script: str, *, root: bool = False) -> int:
        from catalpa_tooling import pgbackrest_db

        return pgbackrest_db.run_db_sh(self.ctx.compose_file, self.env, script, root=root)

    def run_post_restore_hooks(self, target: Literal["app", "metabase"] = "app") -> int:
        from catalpa_tooling import post_db_restore

        run = (
            post_db_restore.run_post_metabase_db_restore_manage_commands
            if target == "metabase"
            else post_db_restore.run_post_db_restore_manage_commands
        )
        return run(self.config, compose_file=self.ctx.compose_file, env_add=self.env, env_name=self.env_name)

    def media_storage(self):
        """The media's named volume or host directory, from the compose file
        (``media_storage.MediaStorage``), resolved once: it may ask docker over SSH. Raises
        ``MediaStorageError``."""
        from catalpa_tooling.media_storage import resolve_media_storage

        if self._media_storage is None:
            self._media_storage = resolve_media_storage(
                compose_file=self.ctx.compose_file, env=self.env, config=self.config
            )
        return self._media_storage

    def describe_media(self) -> str:
        return self.media_storage().describe()

    def preflight_transfer(
        self, *, role: Literal["source", "destination"], db: bool, media: bool, dry_run: bool = False
    ) -> list[str]:
        """``dk_transfer.compose_preflight_errors``: services, external volumes, db up."""
        from catalpa_tooling.dk_transfer import compose_preflight_errors
        from catalpa_tooling.media_storage import MediaStorageError

        storage = None
        if media:
            try:
                storage = self.media_storage()
            except MediaStorageError as exc:
                return [f"{role} (`{self.env_name}`): {exc}"]
        return compose_preflight_errors(
            label=role,
            env_name=self.env_name,
            compose_file=self.ctx.compose_file,
            env_r=self.env,
            media=storage,
            do_db=db,
            do_media=media,
            config=self.config,
            dry_run=dry_run,
        )

    def push_media(self, source: Path, *, dry_run: bool = False) -> int:
        """rsync ``--delete`` into the media's bind directory or volume: over SSH for a remote
        engine, or through a container when the host can't write a local volume's mountpoint.
        No tar fallback (``media_rsync.run_push_media_rsync``)."""
        from catalpa_tooling.media_rsync import run_push_media_rsync

        return run_push_media_rsync(
            self.env,
            source=source,
            dry_run=dry_run,
            method="rsync",
            delete=True,
            config=self.config,
            storage=self.media_storage(),
            label="transfer push",
        )

    def media_rsync_source(self) -> str | None:
        """As ``transfer`` finds it: a bind directory, a remote volume's mountpoint (``docker
        volume inspect`` over SSH), or a local volume's when the host can read it."""
        from catalpa_tooling.media_rsync import resolve_rsync_endpoint

        return resolve_rsync_endpoint(self.env, self.media_storage(), label="fetch media")

    def pull_media(self, target: Path, *, dry_run: bool = False, alpine_image: str = "alpine:3.21") -> int:
        """rsync when the media is reachable; otherwise, or when rsync fails (an SSH user that
        can't read Docker's volumes), ``tar`` through the engine (``media_pull``)."""
        from catalpa_tooling import media_pull, media_rsync
        from catalpa_tooling.media_storage import MediaStorageError

        try:
            storage = self.media_storage()
        except MediaStorageError as exc:
            print(f"fetch media: {exc}", file=sys.stderr)
            return 1
        source = media_rsync.resolve_rsync_endpoint(self.env, storage, label="fetch media")
        if source is not None:
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
            rc = media_rsync.rsync_between_endpoints(source, str(target), delete=False, dry_run=dry_run)
            if rc == 0 or dry_run:
                return rc
            print(f"fetch media: rsync failed (exit {rc}); copying with tar instead.", file=sys.stderr)
        else:
            print(f"fetch media: no rsync route to {storage.describe()}; copying with tar.", file=sys.stderr)
        return media_pull.run_pull_media(
            self.env,
            target=target,
            dry_run=dry_run,
            alpine_image=alpine_image,
            config=self.config,
            storage=storage,
        )

    @classmethod
    def with_env(cls, ctx: ManagedDeployContext, env: dict[str, str]) -> ComposeBackend:
        """A backend whose ``env`` is the one a ``dk <env>`` handler already resolved (compose
        project set, db image named), so every step sees the same values."""
        b = cls(ctx)
        b.__dict__["env"] = env  # the cached_property's slot
        return b

    def refused_subcommand(self, verb: str, sub: str) -> str | None:
        return None

    def ensure_backup_credentials(
        self, target: Literal["pgbackrest", "restic"], *, yes: bool, dry_run: bool, label: str
    ) -> int:
        from catalpa_tooling import compose_backups

        return compose_backups.ensure_backup_credentials(self, target, yes=yes, dry_run=dry_run, label=label)

    def pgbackrest(self, sub: str, ns: argparse.Namespace, *, dry_run: bool) -> int:
        from catalpa_tooling import compose_backups

        return compose_backups.pgbackrest(self, sub, ns, dry_run=dry_run)

    def restic(self, sub: str, ns: argparse.Namespace, *, dry_run: bool) -> int:
        from catalpa_tooling import compose_backups

        return compose_backups.restic(self, sub, ns, dry_run=dry_run)

    def pgbackrest_restore_available(self) -> bool:
        from catalpa_tooling.compose_backups import pgbackrest_restore_configured

        return pgbackrest_restore_configured(self.env)

    def pgbackrest_restore(self, extra: list[str], *, yes: bool, dry_run: bool) -> int:
        from catalpa_tooling import compose_backups

        return compose_backups.pgbackrest_restore(self, extra, yes=yes, dry_run=dry_run)

    def files_restore(self, snapshot: str, *, yes: bool, dry_run: bool) -> int:
        from catalpa_tooling import compose_backups

        return compose_backups.files_restore(self, snapshot, yes=yes, dry_run=dry_run)

    def restore_all(self, extra: list[str], snapshot: str, *, yes: bool, dry_run: bool) -> int:
        from catalpa_tooling import compose_backups

        return compose_backups.restore_all(self, extra, snapshot, yes=yes, dry_run=dry_run)

    def restore_hooks_problem(self, *, metabase: bool) -> str | None:
        return None  # compose runs the hooks with `run --rm`: nothing to render first


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
        raise BackendError(f"{env_name}: no deploy context")
    return ComposeBackend(ctx)
