"""argparse tree for ``native``."""

from __future__ import annotations

import argparse
from pathlib import Path

from catalpa_tooling.cli.completion import attach_choices_completer
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.remote_deploy import list_dk_env_names
from catalpa_tooling.script_discovery import discover_native_commands


def build_native_parser(config: ProjectConfig) -> tuple[argparse.ArgumentParser, set[str]]:
    """Return parser and the set of discovered extension command names."""
    parser = argparse.ArgumentParser(
        prog="native",
        description="Host development without Docker: Django, frontend npm scripts, fetch db/media.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser(
        "fetch",
        help="Fetch DB via `uv run dk <env> db pgdump`, or media via rsync (SSH).",
    )
    fetch_sub = fetch.add_subparsers(dest="resource", required=True)

    default_dk_env = config.default_fetch_dk_env
    legacy_remote_default = (
        config.native.fetch_media.legacy.remote if config.native.fetch_media.legacy else None
    )
    deploy_env_choices = list_dk_env_names(config)

    p_db = fetch_sub.add_parser(
        "db",
        help="Download PostgreSQL custom-format dump via `dk … db pgdump` (requires `uv`; remote `db` up).",
    )
    p_db.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="PATH",
        help="Output file (default: paths.fetch_db_dump from tooling.yaml)",
    )
    env_db = p_db.add_argument(
        "--env",
        default=None,
        metavar="NAME",
        help=f"dk environment under docker/envs/ (default: native.fetch_media.dk_env → {default_dk_env!r})",
    )
    if deploy_env_choices:
        attach_choices_completer(env_db, deploy_env_choices)

    p_media = fetch_sub.add_parser(
        "media",
        help="Sync media via rsync (requires rsync + SSH). Default: django_media volume on docker_host from info.yaml.",
    )
    env_media = p_media.add_argument(
        "--env",
        default=None,
        metavar="NAME",
        help=(
            f"dk env for docker_host / compose_project_name from info.yaml "
            f"(default: native.fetch_media.dk_env → {default_dk_env!r})."
        ),
    )
    if deploy_env_choices:
        attach_choices_completer(env_media, deploy_env_choices)
    p_media.add_argument(
        "--host",
        default=None,
        metavar="USER@HOST",
        help="SSH target (override docker_host from info.yaml, or required for --legacy-path without tooling.yaml ssh_host).",
    )
    p_media.add_argument(
        "--remote",
        default=None,
        metavar="PATH",
        help=(
            "Remote media directory with --legacy-path "
            f"(default: native.fetch_media.legacy.remote"
            f"{f' → {legacy_remote_default!r}' if legacy_remote_default else ''})."
        ),
    )
    p_media.add_argument(
        "--dest",
        type=Path,
        metavar="DIR",
        help=f"Host directory (default: <repo>/{config.native.fetch_media.dest})",
    )
    p_media.add_argument(
        "--partial",
        action="store_true",
        help="Sync only documents/ and original_images/ (skip renditions and other dirs). Default is full tree.",
    )
    p_media.add_argument(
        "--legacy-path",
        action="store_true",
        help="Rsync from native.fetch_media.legacy in tooling.yaml instead of the django_media Docker volume.",
    )
    p_media.add_argument(
        "--compose-project",
        default=None,
        metavar="NAME",
        help="COMPOSE_PROJECT_NAME for volume name <NAME>_django_media (default: compose_project_name from info.yaml).",
    )

    p_run = subparsers.add_parser("runserver", help="Django dev server (uv run manage.py runserver).")
    p_run.add_argument(
        "django_args",
        nargs=argparse.REMAINDER,
        help="Extra args passed to runserver (e.g. 0.0.0.0:8000).",
    )
    p_run.set_defaults(handler="runserver")

    p_manage = subparsers.add_parser("manage", help="Run any Django management command via uv.")
    p_manage.add_argument(
        "manage_args",
        nargs=argparse.REMAINDER,
        help="Arguments to ./manage.py (e.g. migrate, shell_plus).",
    )
    p_manage.set_defaults(handler="manage")

    p_reset = subparsers.add_parser(
        "reset-db",
        help=(
            "dropdb + createdb + PostGIS + migrate (or scripts/native-reset-db-post.sh when present; host Postgres)."
        ),
    )
    p_reset.add_argument(
        "--from-dump",
        metavar="PATH",
        dest="from_dump",
        help=(
            "After recreate, pg_restore this custom-format archive instead of migrate/post-hook. "
            "Extra arguments are forwarded to pg_restore (e.g. --no-owner --no-acl)."
        ),
    )
    p_reset.set_defaults(handler="reset-db")

    p_pgrestore = subparsers.add_parser(
        "pg-restore",
        help="pg_restore from stdin (custom format); uses POSTGRES_* from .env.local like reset-db.",
    )
    p_pgrestore.add_argument(
        "--file",
        metavar="PATH",
        dest="archive_file",
        help="Read the custom-format archive from PATH instead of stdin.",
    )
    p_pgrestore.add_argument(
        "pg_restore_args",
        nargs=argparse.REMAINDER,
        help="Extra pg_restore args (e.g. --clean). --no-owner/--no-acl are added if missing. Default archive is stdin.",
    )
    p_pgrestore.set_defaults(handler="pg-restore")

    subparsers.add_parser(
        "vite",
        help="npm install then Vue dev server (paths.frontend from tooling.yaml).",
    ).set_defaults(handler="vite")

    native_extension_names: set[str] = set()
    native_extensions = discover_native_commands(config.scripts_dir)
    for cmd_name, script_path in native_extensions.items():
        native_extension_names.add(cmd_name)
        rel = script_path.relative_to(config.repo_root)
        p_ext = subparsers.add_parser(
            cmd_name,
            help=f"Run project script {rel} (scripts/native-*.sh).",
        )
        p_ext.add_argument(
            "script_args",
            nargs=argparse.REMAINDER,
            help=f"Arguments forwarded to {script_path.name}.",
        )
        p_ext.set_defaults(handler="native-script", native_script_path=script_path)

    return parser, native_extension_names
