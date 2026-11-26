"""``dk <env> db …``, ``dk <env> files …`` and ``dk <env> restore``, on any runtime (M5).

One dispatcher over ``stack_backend.StackBackend``. What is the same on both runtimes is
written here once:

- the deprecated ``bkp_db`` / ``bkp_files`` spellings;
- refusing subcommands the runtime doesn't have (the backend says which, and what instead);
- refusing a command that backs up on an env that doesn't (``backups:`` in ``info.yaml``);
- creating missing WRITE credentials (the backend knows where they end up);
- ``db restore`` choosing its source (``--file``, ``--dumps``, pgBackRest, then the configured
  dumps, fetched when missing), and restoring a dump (``db_archive.restore_archive``) after one
  confirmation.

What really differs stays per backend: pgBackRest and restic themselves (``pgbackrest``,
``restic``, ``pgbackrest_restore``, ``files_restore``) and ``restore_all`` (quadlet restores
the database and media in one script with the app stopped throughout; compose one after the
other).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Literal

from catalpa_tooling.stack_backend import StackBackend

Verb = Literal["db", "files", "restore"]

_KIND = {"db": "db", "files": "files"}


def run(b: StackBackend, verb: str, ns: argparse.Namespace, *, dry_run: bool) -> int:
    """``dk <env> db|files|restore …`` (``bkp_db`` / ``bkp_files`` too) on ``b``."""
    from catalpa_tooling.deprecation import warn_deprecated

    env = b.env_name
    if verb in ("bkp_db", "bkp_files"):
        new = verb.removeprefix("bkp_")
        warn_deprecated(verb, new, context=f"dk {env}")
        verb = new
    if verb == "restore":
        return b.restore_all(
            _pgbackrest_extra(ns),
            _snapshot(ns),
            yes=bool(getattr(ns, "yes", False)),
            dry_run=dry_run or bool(getattr(ns, "restore_dry_run", False)),
        )

    sub = getattr(ns, f"{verb}_command", None) or getattr(ns, f"bkp_{verb}_command", None)
    label = f"dk {env} {verb} {sub}"
    if not sub:
        print(f"usage: dk {env} {verb} <subcommand> (dk {env} {verb} --help)", file=sys.stderr)
        return 2
    why = b.refused_subcommand(verb, sub)
    if why:
        print(f"{label}: {why}", file=sys.stderr)
        return 2

    yes = bool(getattr(ns, "yes", False))
    target = _write_target(verb, sub, ns)
    if target:
        why = _refuse_write(b, _KIND[verb], label)
        if why:
            print(why, file=sys.stderr)
            return 2
        rc = b.ensure_backup_credentials(target, yes=yes, dry_run=dry_run, label=label)
        if rc != 0:
            return rc

    if verb == "db" and sub == "restore":
        return db_restore(
            b,
            archive=Path(ns.archive_file).expanduser() if getattr(ns, "archive_file", None) else None,
            dumps=bool(getattr(ns, "restore_from_dumps", False)),
            pgbackrest_args=_pgbackrest_extra(ns),
            yes=yes,
            dry_run=dry_run or bool(getattr(ns, "restore_dry_run", False)),
        )
    if verb == "files" and sub == "restore":
        return b.files_restore(_snapshot(ns), yes=yes, dry_run=dry_run)
    if verb == "db":
        return b.pgbackrest(sub, ns, dry_run=dry_run)
    return b.restic(sub, ns, dry_run=dry_run)


def _snapshot(ns: argparse.Namespace) -> str:
    return (getattr(ns, "snapshot", None) or "latest").strip() or "latest"


def _pgbackrest_extra(ns: argparse.Namespace) -> list[str]:
    """pgBackRest restore options after ``--`` (the ``--`` itself dropped)."""
    return [a for a in (getattr(ns, "pgbackrest_restore_args", None) or []) if a != "--"]


def _write_target(verb: str, sub: str, ns: argparse.Namespace) -> Literal["pgbackrest", "restic"] | None:
    """Which WRITE credentials ``verb sub`` needs: it writes to the repository."""
    from catalpa_tooling.doctl_spaces_provision import (
        needs_pgbr_write,
        needs_restic_write,
    )

    if verb == "db":
        mode = getattr(ns, "configure_mode", None) if sub == "configure" else None
        return "pgbackrest" if needs_pgbr_write(sub, [mode] if mode else []) else None
    return "restic" if needs_restic_write(sub) else None


def _refuse_write(b: StackBackend, kind: str, what: str) -> str | None:
    from catalpa_tooling.backup_settings import (
        BackupSettingsError,
        backup_settings,
        load_info,
        refuse_write,
    )

    try:
        return refuse_write(backup_settings(b.config, b.env_name, load_info(b.config, b.env_name)), kind, what)
    except BackupSettingsError as exc:
        return str(exc)


# --- db restore -----------------------------------------------------------------------------


def db_restore(
    b: StackBackend,
    *,
    archive: Path | None = None,
    dumps: bool = False,
    pgbackrest_args: list[str] | None = None,
    yes: bool = False,
    dry_run: bool = False,
) -> int:
    """``dk <env> db restore``: from ``archive`` (``--file``; the app database only), the
    configured dumps (``--dumps``), else pgBackRest when the env has a repository to restore
    from, else the configured dumps, fetched first when missing."""
    from catalpa_tooling.fetch_db import configured_app_dump_exists

    config = b.config
    if archive is not None:
        return restore_dumps(b, [("app", archive)], yes=yes, dry_run=dry_run)
    if dumps:
        rc = _ensure_local_dumps(b, auto_fetch=False)
        return rc if rc != 0 else restore_dumps(b, configured_dumps(config), yes=yes, dry_run=dry_run)
    if b.pgbackrest_restore_available():
        return b.pgbackrest_restore(list(pgbackrest_args or []), yes=yes, dry_run=dry_run)
    if not configured_app_dump_exists(config):
        if dry_run:
            print(
                f"dry-run: no pgBackRest repository and no dump at {config.fetch_db_dump_path}: "
                "would run `dk fetch db`, then restore what it fetched.",
                file=sys.stderr,
            )
            return 0
        rc = _ensure_local_dumps(b, auto_fetch=True)
        if rc != 0:
            return rc
    return restore_dumps(b, configured_dumps(config), yes=yes, dry_run=dry_run)


def configured_dumps(config) -> list[tuple[Literal["app", "metabase"], Path]]:
    """The dumps ``dk fetch db`` writes: the app's, then Metabase's when configured."""
    out: list[tuple[Literal["app", "metabase"], Path]] = [("app", config.fetch_db_dump_path)]
    if config.has_metabase_fetch() and config.fetch_metabase_db_dump_path is not None:
        out.append(("metabase", config.fetch_metabase_db_dump_path))
    return out


_LABEL = {"app": "app", "metabase": "Metabase"}


def restore_dumps(
    b: StackBackend,
    dumps: list[tuple[Literal["app", "metabase"], Path]],
    *,
    yes: bool,
    dry_run: bool,
) -> int:
    """Replace each ``(database, archive)`` in turn, after checking every archive and asking
    once. Refuses before anything is replaced when a hook that will run can't."""
    from catalpa_tooling.pgbackrest_db import pg_dump_archive_error

    env = b.env_name
    problem = b.restore_hooks_problem(metabase=any(t == "metabase" for t, _ in dumps))
    what = " and ".join(f"{env}'s {_LABEL[t]} database with {p}" for t, p in dumps)
    if dry_run:
        print(f"dry-run: would replace {what}", file=sys.stderr)
        if problem:
            print(f"dry-run: a real run would be refused: {problem}", file=sys.stderr)
            return 1
        return 0
    if problem:
        print(f"dk {env} db restore: {problem}", file=sys.stderr)
        return 1
    for _t, path in dumps:
        err = pg_dump_archive_error(path)
        if err:
            print(f"db restore: refusing before dropping anything: {err}", file=sys.stderr)
            return 1
    if not yes:
        if not sys.stdin.isatty():
            print("Refusing db restore without a TTY. Pass --yes for non-interactive use.", file=sys.stderr)
            return 1
        from catalpa_tooling.cli_confirm import confirm_by_typing_env_name

        stops = " The app stops while it runs." if b.dump_restore_stops_app else ""
        print(
            f"WARNING: replace {what}.{stops} The current data is lost.\n  Environment: {b.describe()}",
            file=sys.stderr,
        )
        if not confirm_by_typing_env_name(env):
            print("db restore: cancelled.", file=sys.stderr)
            return 1
    from catalpa_tooling.db_archive import restore_archive

    for target, path in dumps:
        rc = restore_archive(b, Path(path), target=target, stop_app=b.dump_restore_stops_app)
        if rc != 0:
            return rc
    return 0


def _ensure_local_dumps(b: StackBackend, *, auto_fetch: bool) -> int:
    """The configured dumps exist locally; with ``auto_fetch``, fetch the missing ones."""
    from catalpa_tooling.fetch_db import (
        configured_app_dump_exists,
        configured_metabase_dump_exists,
        run_fetch_all_dbs,
    )

    config = b.config
    if configured_app_dump_exists(config):
        if config.has_metabase_fetch() and not configured_metabase_dump_exists(config):
            if not auto_fetch:
                print(
                    "db restore: Metabase dump missing — run `uv run dk fetch db --only metabase` "
                    "or pass `--dumps` only when all configured dumps exist.",
                    file=sys.stderr,
                )
                return 1
            print("db restore: fetching missing Metabase dump …", file=sys.stderr)
            try:
                run_fetch_all_dbs(config, only="metabase")
            except SystemExit as exc:
                return int(exc.code or 1)
        return 0
    if not auto_fetch:
        print(
            f"db restore: no dump at {config.fetch_db_dump_path} — run `uv run dk fetch db` first.",
            file=sys.stderr,
        )
        return 1
    print("db restore: fetching configured databases …", file=sys.stderr)
    try:
        run_fetch_all_dbs(config)
    except SystemExit as exc:
        return int(exc.code or 1)
    if not configured_app_dump_exists(config):
        print("db restore: fetch completed but app dump is still missing or too small.", file=sys.stderr)
        return 1
    return 0
