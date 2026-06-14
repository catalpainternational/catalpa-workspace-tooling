"""argparse tree for ``dk``."""

from __future__ import annotations

import argparse

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.digoc_parser import attach_digoc_subcommands
from catalpa_tooling.dk_transfer import populate_transfer_arguments
from catalpa_tooling.env_parser import attach_env_subparsers
from catalpa_tooling.cli.completion import attach_choices_completer
from catalpa_tooling.remote_deploy import list_deploy_env_names


def build_dk_parser(config: ProjectConfig) -> argparse.ArgumentParser:
    svc_web = config.stack_service("web")
    svc_proxy = config.stack_service("proxy")
    svc_db = config.stack_service("db")
    envs_dir = config.paths.deploy.envs_dir

    parser = argparse.ArgumentParser(
        prog="dk",
        description=(
            f"Run Docker Compose using {envs_dir}/<env>/info.yaml, or build/push the compose stack."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"Examples:\n"
            f"  dk build db\n"
            f"  dk local up -d\n"
            f"  dk local compose logs {svc_web}\n"
            f"  dk prod bkp_db pgdump\n"
            f"  dk local info -e\n"
            f"\n"
            f"Top-level commands (no env): build, push, transfer, digoc.\n"
            f"Environments: directories under {envs_dir}/<name>/ with info.yaml."
        ),
    )
    sub = parser.add_subparsers(dest="dk_command", required=True)

    p_build = sub.add_parser(
        "build",
        help=f"Build {svc_db}, {svc_web}, {svc_proxy} images ({config.compose_prod}).",
    )
    services = p_build.add_argument(
        "services",
        nargs="*",
        choices=(svc_db, svc_web, svc_proxy),
        metavar="SERVICE",
        help="Services to build (default: all three).",
    )
    attach_choices_completer(services, (svc_db, svc_web, svc_proxy))

    p_push = sub.add_parser(
        "push",
        help="Build for linux/amd64 and push images to the registry.",
    )
    p_push.add_argument("--registry", default=None, help="Override registry base.")
    p_push.add_argument("--tag", default=None, help="Override image tag.")
    p_push.add_argument(
        "--repo",
        default=None,
        metavar="OWNER/REPO",
        help="Override GitHub repo for org.opencontainers.image.source.",
    )

    p_transfer = sub.add_parser(
        "transfer",
        help="Copy Postgres + django_media between two environments.",
        description=(
            "Copy PostgreSQL app data and django_media from one docker/envs/ environment to another."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    populate_transfer_arguments(p_transfer, config)
    env_names = list_deploy_env_names(config.deploy_envs_dir)
    for action in p_transfer._actions:
        if action.dest in ("source_env", "dest_env") and env_names:
            attach_choices_completer(action, env_names)

    p_digoc = sub.add_parser("digoc", help="DigitalOcean helpers (wraps host doctl).")
    attach_digoc_subcommands(p_digoc, config)

    attach_env_subparsers(sub, config)

    return parser
