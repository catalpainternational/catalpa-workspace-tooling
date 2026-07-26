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
            f"proxy, cut-release, next-branch, worktree.\n"
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
        help="Cut a final or beta v* tag (dry-run default; never deploys).",
        description=(
            "Cut a tag from the current named branch:\n"
            "  final — on dev-X.Y[.Z]: merge to main, tag vX.Y[.Z], push (suggest next-branch)\n"
            "  beta  — on a named branch: tag vX.Y[.Z].beta.W (infer from dev-*, or --tag)\n"
            "\n"
            "Opening the next dev-* line is a separate command: dk next-branch.\n"
            "Default is dry-run; pass --execute to mutate. Never deploys remote environments."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cut_sub = p_cut.add_subparsers(dest="cut_release_command", required=True)

    def _add_cut_shared(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "-C",
            dest="submodule_path",
            default=None,
            metavar="PATH",
            help="Operate inside this submodule path (e.g. bero).",
        )
        p.add_argument(
            "--execute",
            action="store_true",
            help="Perform git mutations (default: dry-run plan only).",
        )
        p.add_argument(
            "-y",
            "--yes",
            action="store_true",
            help="Skip the interactive confirmation when using --execute.",
        )
        p.add_argument(
            "--allow-dirty",
            action="store_true",
            help="Allow --execute with uncommitted changes (prints a warning).",
        )

    p_final = cut_sub.add_parser(
        "final",
        help="Tag vX.Y[.Z] from current dev-* branch, merge to main, push.",
    )
    _add_cut_shared(p_final)

    p_beta = cut_sub.add_parser(
        "beta",
        help="Tag vX.Y[.Z].beta.W on current named branch tip (no merge to main).",
    )
    p_beta.add_argument(
        "beta_w",
        nargs="?",
        type=int,
        default=None,
        metavar="W",
        help="Beta index (>=1). Default: max existing + 1 when on a parseable dev-* branch.",
    )
    p_beta.add_argument(
        "--tag",
        default=None,
        metavar="TAG",
        help=(
            "Explicit beta tag vX.Y[.Z].beta.W. Required when the branch is not "
            "dev-X.Y[.Z]; optional override on a dev-* branch (mutually exclusive with W)."
        ),
    )
    _add_cut_shared(p_beta)

    p_next = sub.add_parser(
        "next-branch",
        help="Open the next dev-* line from a final v* tag tip (dry-run default).",
        description=(
            "Create/push the next dev-* branch from HEAD when HEAD is at a final "
            "vX.Y[.Z] tag tip (detached or any branch, e.g. main after cut-release final).\n"
            "\n"
            "Default is dry-run; pass --execute to mutate."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_next.add_argument(
        "spec",
        metavar="BUMP|BRANCH",
        help="major|minor|hotfix, or an explicit dev-X.Y[.Z] name.",
    )
    p_next.add_argument(
        "--set-default",
        action="store_true",
        help="After creating the branch, set it as the GitHub default.",
    )
    _add_cut_shared(p_next)

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
