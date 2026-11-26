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
    if verb == "files" and sub in _RESTIC_READ_ONLY:
        why = _refuse_unconfigured(b, label)
        if why:
            print(why, file=sys.stderr)
            return 2
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
    """pgBackRest restore options after ``--``: the leading ``--`` dropped, anything after it
    (a later ``--`` included) passed on as given."""
    args = list(getattr(ns, "pgbackrest_restore_args", None) or [])
    while args and args[0] == "--":
        args.pop(0)
    return args


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


_RESTIC_READ_ONLY = ("snapshots", "check", "stats")


def _refuse_unconfigured(b: StackBackend, what: str) -> str | None:
    """Why a restic command that only reads has nothing to read: the env neither backs up its
    files nor restores another env's (instead of restic's own "RESTIC_REPOSITORY is required")."""
    from catalpa_tooling.backup_settings import BackupSettingsError, backup_settings, load_info

    try:
        s = backup_settings(b.config, b.env_name, load_info(b.config, b.env_name))
    except BackupSettingsError as exc:
        return str(exc)
    if s.writes("files") or s.reads("files"):
        return None
    return (
        f"{what}: {s.env} neither backs up nor restores its files (restic). To back up, add "
        f"`backups: [{', '.join(sorted(s.backups | {'files'}))}]` to docker/envs/{s.env}/info.yaml; "
        "to restore another env's backups, set `restore_from:` there."
    )


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
    from, else the configured dumps, fetched first when missing.

    An env whose database is backed up or restored with pgBackRest (``backups: [db]`` or
    ``restore_from``) never falls back to dumps silently: when the backend can't find its
    repository, that's a misconfiguration (missing credentials, a stale render), so it refuses.
    """
    from catalpa_tooling.fetch_db import configured_app_dump_exists

    config = b.config
    if archive is not None:
        return restore_dumps(b, [("app", archive)], yes=yes, dry_run=dry_run)
    if dumps:
        rc = _ensure_local_dumps(b, auto_fetch=False)
        return rc if rc != 0 else restore_dumps(b, configured_dumps(config), yes=yes, dry_run=dry_run)
    if b.pgbackrest_restore_available():
        return b.pgbackrest_restore(list(pgbackrest_args or []), yes=yes, dry_run=dry_run)
    why = _pgbackrest_expected(b)
    if why:
        print(
            f"dk {b.env_name} db restore: {why}, but no pgBackRest repository is configured for it "
            f"(credentials, or on a rendered host the render). Fix that, or restore a dump "
            "explicitly with --file PATH or --dumps.",
            file=sys.stderr,
        )
        return 1
    fetch = not configured_app_dump_exists(config)
    return restore_dumps(b, configured_dumps(config), yes=yes, dry_run=dry_run, fetch=fetch)


def _pgbackrest_expected(b: StackBackend) -> str | None:
    """Why this env's database should restore with pgBackRest (``backups`` / ``restore_from``),
    or None when dumps are its normal source."""
    from catalpa_tooling.backup_settings import (
        BackupSettingsError,
        backup_settings,
        load_info,
    )

    try:
        roles = backup_settings(b.config, b.env_name, load_info(b.config, b.env_name))
    except FileNotFoundError:
        return None  # no info.yaml (a bare test or local env): nothing says pgBackRest
    except BackupSettingsError as exc:
        return str(exc)
    if roles.writes("db"):
        return f"{b.env_name} backs up its database (backups: [db])"
    if roles.reads("db"):
        return f"{b.env_name} restores {roles.restore_from}'s database (restore_from: {roles.restore_from})"
    return None


def configured_dumps(config) -> list[tuple[Literal["app", "metabase"], Path]]:
    """The dumps ``dk fetch db`` writes: the app's, then Metabase's when configured."""
    out: list[tuple[Literal["app", "metabase"], Path]] = [("app", config.fetch_db_dump_path)]
    if config.has_metabase_fetch() and config.fetch_metabase_db_dump_path is not None:
        out.append(("metabase", config.fetch_metabase_db_dump_path))
    return out


_LABEL = {"app": "app", "metabase": "Metabase"}


def _archive_problem(dumps: list[tuple[str, Path]]) -> str | None:
    from catalpa_tooling.pgbackrest_db import pg_dump_archive_error

    for _t, path in dumps:
        err = pg_dump_archive_error(path)
        if err:
            return err
    return None


def restore_dumps(
    b: StackBackend,
    dumps: list[tuple[Literal["app", "metabase"], Path]],
    *,
    yes: bool,
    dry_run: bool,
    fetch: bool = False,
) -> int:
    """Replace each ``(database, archive)``, after checking every archive and asking once.

    Everything that can refuse runs first (archives, hooks, the confirmation), and only then,
    with ``fetch``, ``dk fetch db`` brings the dumps. The app stops once around all of them
    (when the backend stops it at all), and the post-restore hooks run after the last one, so
    the app never serves a half-restored cluster. If a restore fails the app stays stopped; if
    it does not start again afterwards, the command says so and fails.
    """
    from catalpa_tooling.db_archive import restore_archive

    env = b.env_name
    problem = b.restore_hooks_problem(metabase=any(t == "metabase" for t, _ in dumps))
    archive_err = None if fetch else _archive_problem(dumps)
    what = " and ".join(f"{env}'s {_LABEL[t]} database with {p}" for t, p in dumps)
    if dry_run:
        if fetch:
            print(f"dry-run: no dump at {b.config.fetch_db_dump_path}: would run `dk fetch db`, then", file=sys.stderr)
        print(f"dry-run: would replace {what}", file=sys.stderr)
        refused = problem or (f"refusing before dropping anything: {archive_err}" if archive_err else None)
        if refused:
            print(f"dry-run: a real run would be refused: {refused}", file=sys.stderr)
            return 1
        return 0
    if problem:
        print(f"dk {env} db restore: {problem}", file=sys.stderr)
        return 1
    if archive_err:
        print(f"db restore: refusing before dropping anything: {archive_err}", file=sys.stderr)
        return 1
    if not yes:
        if not sys.stdin.isatty():
            print("Refusing db restore without a TTY. Pass --yes for non-interactive use.", file=sys.stderr)
            return 1
        from catalpa_tooling.cli_confirm import confirm_by_typing_env_name

        stops = " The app stops while it runs." if b.dump_restore_stops_app else ""
        fetched = " (fetched first with `dk fetch db`)" if fetch else ""
        print(
            f"WARNING: replace {what}{fetched}.{stops} The current data is lost.\n  Environment: {b.describe()}",
            file=sys.stderr,
        )
        if not confirm_by_typing_env_name(env):
            print("db restore: cancelled.", file=sys.stderr)
            return 1
    if fetch:
        rc = _ensure_local_dumps(b, auto_fetch=True)
        if rc != 0:
            return rc
        archive_err = _archive_problem(dumps)
        if archive_err:
            print(f"db restore: refusing before dropping anything: {archive_err}", file=sys.stderr)
            return 1

    stopped = b.stop_writers() if b.dump_restore_stops_app else None
    restored = False
    try:
        for target, path in dumps:
            rc = restore_archive(b, Path(path), target=target, stop_app=False, run_hooks=False)
            if rc != 0:
                return rc
        restored = True
    finally:
        # A failed or interrupted restore leaves the app stopped: better down than serving a
        # database that is dropped or half restored.
        if b.dump_restore_stops_app and not restored:
            print(
                f"db restore failed: {env}'s app is left stopped. "
                f"Fix the database, then run `dk {env} start`.",
                file=sys.stderr,
            )
    start_rc = b.start_writers(stopped) if b.dump_restore_stops_app else 0
    # The hooks run in one-shot containers, so they don't wait on the app coming back.
    for target, _path in dumps:
        rc = b.run_post_restore_hooks(target)
        if rc != 0:
            return rc
    if start_rc != 0:
        print(
            f"db restore: the database is restored, but {env}'s app did not start again "
            f"(exit {start_rc}). Check it, then run `dk {env} start`.",
            file=sys.stderr,
        )
        return start_rc
    print("db restore: done.", file=sys.stderr)
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
