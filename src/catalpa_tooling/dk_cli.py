"""argparse entrypoint for dk: build/push stack images, or Docker Compose per docker/envs/<env>."""

import argparse
import sys

from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.local_compose import _cmd_build
from catalpa_tooling.build_push import _cmd_push
from catalpa_tooling.dk_transfer import build_transfer_arg_parser, cmd_transfer
from catalpa_tooling.remote_deploy import _cmd_deploy, _normalize_dk_env_argv, list_deploy_env_names


def _print_dk_help(config: ProjectConfig) -> None:
    web = config.stack_service("web")
    proxy = config.stack_service("proxy")
    db = config.stack_service("db")
    print(
        f"""usage: dk [-h] {{build | push | <env>}} ...

Run Docker Compose using {config.paths.deploy.envs_dir}/<env>/info.yaml, or build/push the compose stack.

  dk build [SERVICE ...]     Build {db}, {web}, {proxy} images ({config.compose_prod}; see {config.paths.deploy.images_config}).
  dk push [--registry ...]   Build for linux/amd64 and push images to the registry.
  dk transfer [OPTS] SRC DST Copy Postgres + django_media from SRC env to DST (see docs/DK.md).
  dk digoc SUBCMD ...        DigitalOcean: auth, projects, droplets, cloud-config (wraps host doctl).
  dk <env> [ARGS ...]        Environment with {config.paths.deploy.envs_dir}/<env>/info.yaml (default ARGS: up -d).
                             Examples: dk local up -d, dk local --tag v1 up -d, dk local info, dk local info -e,
                             dk local secrets, dk prod host --write, dk local manage migrate, dk local trust-caddy-cert,
                             dk demo wipe

The only commands without an environment name are build, push, transfer, and digoc."""
    )


def _main_impl() -> None:
    argv = sys.argv[1:]
    config = ProjectConfig.from_cwd()

    if not argv:
        _print_dk_help(config)
        sys.exit(1)
    if argv[0] in ("-h", "--help"):
        _print_dk_help(config)
        sys.exit(0)

    first = argv[0]
    svc_web = config.stack_service("web")
    svc_proxy = config.stack_service("proxy")
    svc_db = config.stack_service("db")

    if first == "build":
        p = argparse.ArgumentParser(
            prog="dk build",
            description=f"Build {config.compose_prod} stack images.",
        )
        p.add_argument(
            "services",
            nargs="*",
            choices=(svc_db, svc_web, svc_proxy),
            metavar="SERVICE",
            help="Services to build (default: all three). Example: dk build db",
        )
        ns = p.parse_args(argv[1:])
        sys.exit(_cmd_build(config.compose_prod, argparse.Namespace(services=ns.services), config))

    if first == "push":
        p = argparse.ArgumentParser(
            prog="dk push",
            description=f"Build {config.compose_prod} stack for linux/amd64, then push to a registry.",
        )
        p.add_argument(
            "--registry",
            default=None,
            help="Override: registry base (default: from images config).",
        )
        p.add_argument(
            "--tag",
            default=None,
            help="Override: image tag (default: from images config or git describe).",
        )
        p.add_argument(
            "--repo",
            default=None,
            metavar="OWNER/REPO",
            help="Override: GitHub repo for org.opencontainers.image.source (default: from git remote).",
        )
        ns = p.parse_args(argv[1:])
        sys.exit(_cmd_push(config.compose_prod, ns, config))

    if first == "transfer":
        p = build_transfer_arg_parser(config)
        ns = p.parse_args(argv[1:])
        sys.exit(cmd_transfer(ns, config))

    if first == "digoc":
        from catalpa_tooling.doctl_cli import run_digoc

        sys.exit(run_digoc(argv[1:]))

    envs = list_deploy_env_names(config.deploy_envs_dir)
    if first not in envs:
        print(
            f"dk: unknown command or environment {first!r}. "
            f"Use `dk build`, `dk push`, `dk transfer`, `dk digoc`, or a name with "
            f"{config.paths.deploy.envs_dir}/<name>/info.yaml.",
            file=sys.stderr,
        )
        if envs:
            print("Available environments:", ", ".join(envs), file=sys.stderr)
        else:
            print(
                f"No environments found (add {config.paths.deploy.envs_dir}/<name>/info.yaml).",
                file=sys.stderr,
            )
        sys.exit(1)

    parser = argparse.ArgumentParser(
        prog="dk",
        description=f"Docker Compose for {config.paths.deploy.envs_dir}/<env>/info.yaml (DOCKER_HOST, credentials, …).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print resolved DOCKER_HOST and SITE_ORIGIN, do not run docker compose.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip interactive confirmation for destructive down -v / wipe / restores (non-TTY needs this).",
    )
    parser.add_argument(
        "--tag",
        default=None,
        metavar="TAG",
        help="Override stack image tag (default: image_tag from env info.yaml, else git/images.yaml).",
    )
    parser.add_argument(
        "env_name",
        help=f"Environment name (e.g. local, staging). Directory {config.paths.deploy.envs_dir}/<env_name>/ must exist.",
    )
    parser.add_argument(
        "compose_args",
        nargs=argparse.REMAINDER,
        metavar="COMMAND [ARG ...]",
        help="Docker Compose args (default: up -d). `wipe` => down -v. Also: info, secrets, ensure_volumes, trust-caddy-cert, manage, pull_media, bkp_files (push, restic, …), bkp_db, zabbix.",
    )
    args = parser.parse_args(_normalize_dk_env_argv(argv))
    sys.exit(_cmd_deploy(args, config))


def main() -> None:
    run_cli(_main_impl, label="dk")
