"""Top-level ``dk fetch`` commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.fetch_db import run_fetch_all_dbs
from catalpa_tooling.fetch_media import run_fetch_media


def populate_fetch_arguments(parser: argparse.ArgumentParser, config: ProjectConfig) -> None:
    sub = parser.add_subparsers(dest="fetch_resource", required=True)

    default_dk_env = config.default_fetch_dk_env
    p_db = sub.add_parser(
        "db",
        help="Download configured PostgreSQL dumps to paths.fetch_*_dump.",
    )
    p_db.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="Output file for the app database only (default: paths.fetch_db_dump).",
    )
    p_db.add_argument(
        "--env",
        dest="dk_env",
        default=None,
        metavar="NAME",
        help=f"Source docker/envs/<name> for via: dk (default: {default_dk_env!r}).",
    )
    p_db.add_argument(
        "--only",
        default=None,
        metavar="KEY",
        help="Fetch a single database key from native.fetch.databases (e.g. app, metabase).",
    )

    legacy_remote = (
        config.native.fetch_media.legacy.remote if config.native.fetch_media.legacy else None
    )
    legacy_default = (
        config.native.fetch_media.legacy.default if config.native.fetch_media.legacy else False
    )
    p_media = sub.add_parser("media", help="Rsync django_media from a deploy host.")
    p_media.add_argument("--host", default=None, metavar="USER@HOST")
    p_media.add_argument(
        "--env",
        dest="dk_env",
        default=None,
        metavar="NAME",
        help=f"docker/envs/<name> when resolving Docker volume (default: {default_dk_env!r}).",
    )
    p_media.add_argument(
        "--dest",
        type=Path,
        default=None,
        help=f"Local directory (default: {config.native.fetch_media.dest}).",
    )
    p_media.add_argument("--partial", action="store_true")
    p_media.add_argument(
        "--legacy-path",
        action=argparse.BooleanOptionalAction,
        default=legacy_default,
    )
    p_media.add_argument("--remote", default=legacy_remote, metavar="PATH")
    p_media.add_argument("--compose-project", default=None, metavar="NAME")


def cmd_fetch(ns: argparse.Namespace, config: ProjectConfig) -> int:
    if ns.fetch_resource == "db":
        overrides: dict[str, Path] = {}
        if ns.output is not None:
            overrides["app"] = ns.output
        try:
            run_fetch_all_dbs(
                config,
                dk_env=ns.dk_env,
                only=ns.only,
                output_overrides=overrides or None,
            )
        except SystemExit as exc:
            return int(exc.code or 1)
        return 0

    if ns.fetch_resource == "media":
        env_name = ns.dk_env if ns.dk_env is not None else config.default_fetch_dk_env
        try:
            run_fetch_media(
                config,
                dk_env=env_name,
                host=ns.host,
                dest=ns.dest,
                partial=bool(ns.partial),
                legacy_path=bool(ns.legacy_path),
                legacy_remote=ns.remote,
                compose_project=ns.compose_project,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"dk fetch media: {exc}", file=sys.stderr)
            return 1
        except SystemExit as exc:
            return int(exc.code or 1)
        return 0

    print(f"dk fetch: unknown resource {ns.fetch_resource!r}", file=sys.stderr)
    return 1
