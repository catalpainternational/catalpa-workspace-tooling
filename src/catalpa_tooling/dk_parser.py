"""argparse tree for ``dk``."""

from __future__ import annotations

import argparse

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.digoc_parser import attach_digoc_subcommands
from catalpa_tooling.dk_transfer import populate_transfer_arguments
from catalpa_tooling.dk_fetch import populate_fetch_arguments
from catalpa_tooling.env_parser import attach_env_subparsers
from catalpa_tooling.local_proxy import LOCAL_PROXY_CONTAINER
from catalpa_tooling.cli.completion import attach_choices_completer
from catalpa_tooling.remote_deploy import list_deploy_env_names
from catalpa_tooling.worktree_cli import attach_worktree_subcommands


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
            f"  dk full up -d\n"
            f"  dk --worktree onboarding dev up -d\n"
            f"  dk full compose logs {svc_web}\n"
            f"  dk prod db pgdump\n"
            f"  dk full info -e\n"
            f"\n"
            f"Top-level commands (no env): build, push, clean-images, transfer, fetch, digoc, "
            f"proxy, cut-release, worktree.\n"
            f"Environments: directories under {envs_dir}/<name>/ with info.yaml.\n"
            f"Use --worktree / -W <slug> from the main checkout to target .worktrees/<slug> "
            f"without cd (no direnv)."
        ),
    )
    parser.add_argument(
        "--worktree",
        "-W",
        dest="worktree_slug",
        default=None,
        metavar="SLUG",
        help=(
            "Run against .worktrees/<SLUG> (load that checkout's tooling + overlay). "
            "Peeled before subcommands; prefer `dk --worktree SLUG <env> …` from the main repo."
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
        help="Build for linux/amd64, push images, and attach CycloneDX SBOMs.",
    )
    p_push.add_argument("--registry", default=None, help="Override registry base.")
    p_push.add_argument("--tag", default=None, help="Override image tag.")
    p_push.add_argument(
        "--repo",
        default=None,
        metavar="OWNER/REPO",
        help="Override GitHub repo for org.opencontainers.image.source.",
    )
    p_push.add_argument(
        "--no-sbom",
        action="store_true",
        help="Skip Syft scan and ORAS SBOM attach after push.",
    )

    p_clean = sub.add_parser(
        "clean-images",
        help="Remove old GHCR package versions (dry-run by default).",
    )
    p_clean.add_argument(
        "--apply",
        action="store_true",
        help="Delete staged package versions (default is dry-run).",
    )
    p_clean.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation when using --apply.",
    )
    p_clean.add_argument(
        "--keep-n-tagged",
        type=int,
        default=None,
        metavar="N",
        help="Keep N newest tagged versions older than --older-than.",
    )
    p_clean.add_argument(
        "--older-than",
        default=None,
        metavar="INTERVAL",
        help="Retention window for tagged images (e.g. '180 days').",
    )
    p_clean.add_argument(
        "--delete-untagged",
        choices=("true", "false"),
        default=None,
        help="Delete untagged package versions.",
    )
    p_clean.add_argument(
        "--package",
        default=None,
        metavar="NAME",
        help="Clean a single GHCR package (default: all stack components).",
    )
    p_clean.add_argument(
        "--token",
        default=None,
        help="GitHub token override (default: GH_TOKEN, GITHUB_TOKEN, or gh auth token).",
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

    p_fetch = sub.add_parser(
        "fetch",
        help="Download production DB dumps and/or media into the repo.",
    )
    populate_fetch_arguments(p_fetch, config)
    for action in p_fetch._actions:
        if action.dest == "dk_env" and env_names:
            attach_choices_completer(action, env_names)

    p_digoc = sub.add_parser("digoc", help="DigitalOcean helpers (wraps host doctl).")
    attach_digoc_subcommands(p_digoc, config)

    p_proxy = sub.add_parser(
        "proxy",
        help="Machine-wide local dev HTTPS reverse proxy (*.localdev.temp.build).",
    )
    p_proxy.add_argument("--dry-run", action="store_true", help="Show actions without running.")
    proxy_sub = p_proxy.add_subparsers(dest="proxy_command", required=True)
    proxy_sub.add_parser("up", help=f"Start {LOCAL_PROXY_CONTAINER} (Caddy on :80/:443).")
    proxy_sub.add_parser("down", help=f"Stop and remove {LOCAL_PROXY_CONTAINER}.")
    proxy_sub.add_parser("status", help="Show proxy container and route count.")
    proxy_sub.add_parser(
        "trust",
        help="Trust Caddy local CA from the global proxy (macOS/Linux; requires sudo).",
    )
    proxy_sub.add_parser(
        "ca",
        help="Show LAN CA download URL + QR and device install steps.",
    )

    p_cut = sub.add_parser(
        "cut-release",
        help="Cut a release tag, open the next dev-* line, or push a staging beta tag.",
        description=(
            "Three modes:\n"
            "  A) On dev-X.Y[.Z] + --bump: merge to main, tag vX.Y[.Z], create next branch.\n"
            "  B) On vX.Y[.Z] tag + --bump: create next branch only.\n"
            "  C) On dev-X.Y[.Z] + --beta: tag vX.Y[.Z].beta.W on branch tip (no next branch).\n"
            "\n"
            "Default is dry-run; pass --execute to mutate. Never deploys remote environments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_cut.add_argument(
        "--bump",
        choices=("major", "minor", "hotfix"),
        default=None,
        help="Next-branch bump (Mode A/B). Mutually exclusive with --beta.",
    )
    p_cut.add_argument(
        "--beta",
        action="store_true",
        help="Mode C: cut vX.Y.Z.beta.W on the current dev-* tip (no next branch).",
    )
    p_cut.add_argument(
        "--beta-w",
        type=int,
        default=None,
        metavar="N",
        help="Explicit beta W (default: max existing + 1).",
    )
    p_cut.add_argument(
        "--submodule",
        default=None,
        metavar="PATH",
        help="Operate inside this submodule path (e.g. bero).",
    )
    p_cut.add_argument(
        "--execute",
        action="store_true",
        help="Perform git/gh mutations (default: dry-run plan only).",
    )
    p_cut.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation when using --execute.",
    )
    p_cut.add_argument(
        "--set-default",
        action="store_true",
        help="After creating the next branch, set it as the GitHub default (Mode A/B).",
    )
    p_cut.add_argument(
        "--tag",
        default=None,
        metavar="TAG",
        help="Override release or beta tag.",
    )
    p_cut.add_argument(
        "--next-branch",
        default=None,
        metavar="BRANCH",
        help="Override next dev-* branch name (Mode A/B).",
    )
    p_cut.add_argument(
        "--pin-submodule",
        action="append",
        default=None,
        metavar="PATH=REF",
        help="Checkout REF in submodule PATH and commit the gitlink (repeatable).",
    )
    p_cut.add_argument(
        "--image-env",
        default=None,
        metavar="NAME",
        help="Set docker/envs/<NAME>/info.yaml image_tag to the new tag.",
    )
    p_cut.add_argument(
        "--allow-prod-beta",
        action="store_true",
        help="Allow --image-env prod together with --beta (default: refuse).",
    )
    p_cut.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow --execute with uncommitted changes (prints a warning).",
    )

    p_worktree = sub.add_parser(
        "worktree",
        help="Isolated git worktrees for local dk dev (own DB volumes + domains).",
        description=(
            "Create checkouts under .worktrees/<slug>/ with a gitignored "
            ".catalpa-worktree.yaml overlay that remaps COMPOSE_PROJECT_NAME and "
            "localdev hostnames for dk <base-env> (default: dev)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    attach_worktree_subcommands(p_worktree)

    attach_env_subparsers(sub, config)

    return parser
