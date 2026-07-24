"""Thin argparse glue for ``dk worktree`` (Typer-compatible logic in ``worktree.py``)."""

from __future__ import annotations

import argparse
from pathlib import Path

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.worktree import (
    worktree_context,
    worktree_create,
    worktree_down,
    worktree_info,
    worktree_list,
    worktree_logs,
    worktree_remove,
    worktree_restart,
    worktree_seed,
    worktree_status,
    worktree_up,
)
from catalpa_tooling.worktree_overlay import DEFAULT_BASE_ENV


def attach_worktree_subcommands(parser: argparse.ArgumentParser) -> None:
    """Attach worktree lifecycle subcommands under a ``dk worktree`` parser."""
    sub = parser.add_subparsers(dest="worktree_command", required=True)

    p_create = sub.add_parser(
        "create",
        help="Create a git worktree under .worktrees/<slug> with an isolated dev overlay.",
    )
    p_create.add_argument("slug", help="Worktree name (becomes .worktrees/<slug>).")
    p_create.add_argument(
        "--branch",
        default=None,
        metavar="NAME",
        help="Branch to check out (created if missing). Default: worktree/<slug>.",
    )
    p_create.add_argument(
        "--base-branch",
        default=None,
        metavar="REF",
        help="Start point for a new branch (default: HEAD).",
    )
    p_create.add_argument(
        "--base-env",
        default=DEFAULT_BASE_ENV,
        help=f"Local env to remap (default: {DEFAULT_BASE_ENV}).",
    )
    p_create.add_argument("--dry-run", action="store_true", help="Show actions without running.")
    p_create.add_argument(
        "--no-submodules",
        action="store_true",
        help="Skip git submodule update after worktree add.",
    )
    p_create.add_argument(
        "--full-submodules",
        action="store_true",
        help=(
            "Clone submodules with full history (default is shallow --depth 1; "
            "local main-checkout --reference is still preferred when available)."
        ),
    )
    p_create.add_argument(
        "--no-seed",
        action="store_true",
        help="Skip seeding DB + host media from the main checkout's base env.",
    )
    p_create.add_argument(
        "--up",
        action="store_true",
        help="After create (and seed unless --no-seed), run dk worktree up for this slug.",
    )

    sub.add_parser("list", help="List worktrees under .worktrees/.")

    p_info = sub.add_parser(
        "info",
        help="Show overlay details for the current worktree or a named slug.",
    )
    p_info.add_argument(
        "slug",
        nargs="?",
        default=None,
        help="Worktree slug (default: current checkout if it has a marker).",
    )

    p_up = sub.add_parser(
        "up",
        help="Ensure local proxy and bring up the remapped worktree stack.",
    )
    p_up.add_argument(
        "slug",
        nargs="?",
        default=None,
        help="Worktree slug (default: current checkout if it has a marker).",
    )
    p_up.add_argument("--dry-run", action="store_true", help="Show actions without running.")

    p_down = sub.add_parser(
        "down",
        help="Compose down for the worktree stack (keeps volumes).",
    )
    p_down.add_argument(
        "slug",
        nargs="?",
        default=None,
        help="Worktree slug (default: current checkout if it has a marker).",
    )
    p_down.add_argument("--dry-run", action="store_true", help="Show actions without running.")

    p_restart = sub.add_parser(
        "restart",
        help="Compose restart for the worktree stack.",
    )
    p_restart.add_argument(
        "slug",
        nargs="?",
        default=None,
        help="Worktree slug (default: current checkout if it has a marker).",
    )
    p_restart.add_argument(
        "--service",
        dest="services",
        action="append",
        default=None,
        metavar="NAME",
        help="Restart only these compose services (repeatable).",
    )
    p_restart.add_argument("--dry-run", action="store_true", help="Show actions without running.")

    p_logs = sub.add_parser(
        "logs",
        help="Compose logs for the worktree stack.",
    )
    p_logs.add_argument(
        "slug",
        nargs="?",
        default=None,
        help="Worktree slug (default: current checkout if it has a marker).",
    )
    p_logs.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Follow log output.",
    )
    p_logs.add_argument(
        "services",
        nargs="*",
        default=None,
        help="Optional compose service names.",
    )
    p_logs.add_argument("--dry-run", action="store_true", help="Show actions without running.")

    p_status = sub.add_parser(
        "status",
        help="Show overlay + stack status and compose ps summary.",
    )
    p_status.add_argument(
        "slug",
        nargs="?",
        default=None,
        help="Worktree slug (default: current checkout if it has a marker).",
    )

    p_context = sub.add_parser(
        "context",
        help="Print worktree identity for humans or agents.",
    )
    p_context.add_argument(
        "slug",
        nargs="?",
        default=None,
        help="Worktree slug (default: current checkout if it has a marker).",
    )
    p_context.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )

    p_remove = sub.add_parser(
        "remove",
        help="Remove a git worktree (optionally wipe its Docker volumes).",
    )
    p_remove.add_argument("slug", help="Worktree slug under .worktrees/.")
    p_remove.add_argument(
        "--wipe",
        action="store_true",
        help="Also compose down -v for the remapped compose project.",
    )
    p_remove.add_argument("--dry-run", action="store_true", help="Show actions without running.")
    p_remove.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation (required without a TTY).",
    )

    p_seed = sub.add_parser(
        "seed",
        help="Copy DB + host media from the main checkout's base env into a worktree.",
    )
    p_seed.add_argument(
        "slug",
        nargs="?",
        default=None,
        help="Worktree slug under .worktrees/ (optional when already inside that worktree).",
    )
    p_seed.add_argument(
        "--db",
        action="store_true",
        help="Select the database leg (default: both unless only --media).",
    )
    p_seed.add_argument(
        "--media",
        action="store_true",
        help="Select the host media leg (default: both unless only --db).",
    )
    p_seed.add_argument(
        "--from",
        dest="from_path",
        default=None,
        metavar="DIR",
        help="Main checkout path (default: parent_repo_root from the marker).",
    )
    p_seed.add_argument("--dry-run", action="store_true", help="Show actions without running.")
    p_seed.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation (required without a TTY).",
    )


def cmd_worktree(ns: argparse.Namespace, config: ProjectConfig) -> int:
    """Dispatch ``dk worktree`` subcommands."""
    cmd = ns.worktree_command
    if cmd == "create":
        return worktree_create(
            config,
            slug=ns.slug,
            branch=ns.branch,
            base_branch=ns.base_branch,
            base_env=ns.base_env,
            dry_run=bool(ns.dry_run),
            init_submodules=not bool(ns.no_submodules),
            shallow_submodules=not bool(ns.full_submodules),
            seed=not bool(ns.no_seed),
            bring_up=bool(ns.up),
        )
    if cmd == "list":
        return worktree_list(config)
    if cmd == "info":
        return worktree_info(config, slug=ns.slug)
    if cmd == "up":
        return worktree_up(config, slug=ns.slug, dry_run=bool(ns.dry_run))
    if cmd == "down":
        return worktree_down(config, slug=ns.slug, dry_run=bool(ns.dry_run))
    if cmd == "restart":
        return worktree_restart(
            config,
            slug=ns.slug,
            services=ns.services,
            dry_run=bool(ns.dry_run),
        )
    if cmd == "logs":
        return worktree_logs(
            config,
            slug=ns.slug,
            follow=bool(ns.follow),
            services=ns.services,
            dry_run=bool(ns.dry_run),
        )
    if cmd == "status":
        return worktree_status(config, slug=ns.slug)
    if cmd == "context":
        return worktree_context(config, slug=ns.slug, as_json=bool(ns.json))
    if cmd == "remove":
        return worktree_remove(
            config,
            slug=ns.slug,
            wipe=bool(ns.wipe),
            dry_run=bool(ns.dry_run),
            yes=bool(ns.yes),
        )
    if cmd == "seed":
        do_db = do_media = True
        if ns.db and not ns.media:
            do_db, do_media = True, False
        elif ns.media and not ns.db:
            do_db, do_media = False, True
        elif ns.db and ns.media:
            do_db = do_media = True
        from_path = Path(ns.from_path).expanduser() if ns.from_path else None
        return worktree_seed(
            config,
            slug=ns.slug,
            do_db=do_db,
            do_media=do_media,
            from_path=from_path,
            dry_run=bool(ns.dry_run),
            yes=bool(ns.yes),
        )
    print(f"dk worktree: unknown command {cmd!r}", file=__import__("sys").stderr)
    return 1
