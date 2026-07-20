"""argparse entrypoint for dk: build/push stack images, or Docker Compose per docker/envs/<env>."""

import argparse
import sys

from catalpa_tooling.cli.completion import activate, argcomplete_active
from catalpa_tooling.cli.dk_argv import (
    build_implicit_compose_namespace,
    is_implicit_compose_argv,
    normalize_dk_env_argv,
    normalize_dk_root_argv,
)
from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.local_compose import _cmd_build
from catalpa_tooling.build_push import _cmd_push
from catalpa_tooling.clean_images import clean_images
from catalpa_tooling.dk_transfer import cmd_transfer
from catalpa_tooling.dk_fetch import cmd_fetch
from catalpa_tooling.dk_parser import build_dk_parser
from catalpa_tooling.doctl_cli import dispatch_digoc
from catalpa_tooling.env_handlers import handle_env_command
from catalpa_tooling.local_proxy_cli import cmd_proxy
from catalpa_tooling.cut_release import cut_release
from catalpa_tooling.remote_deploy import list_dk_env_names, list_deploy_env_names, resolve_deploy_env_name


def _parse_dk(config: ProjectConfig, argv: list[str], parser: argparse.ArgumentParser) -> argparse.Namespace:
    root_argv = normalize_dk_root_argv(argv)
    if not root_argv:
        parser.print_help()
        raise SystemExit(1)

    first = root_argv[0]
    envs = set(list_dk_env_names(config))

    if first in envs:
        env_argv = normalize_dk_env_argv(root_argv)
        if env_argv == ["--help"]:
            env_name = root_argv[0]
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    env_parser = action.choices.get(env_name)
                    if env_parser is not None:
                        env_parser.print_help()
                        raise SystemExit(0)
            parser.print_help()
            raise SystemExit(0)
        if is_implicit_compose_argv(root_argv) and not argcomplete_active():
            return build_implicit_compose_namespace(root_argv)
        return parser.parse_args(env_argv)

    return parser.parse_args(root_argv)


def _main_impl() -> None:
    config = ProjectConfig.from_cwd()
    parser = build_dk_parser(config)
    # Must run before custom argv routing: argcomplete uses COMP_LINE while sys.argv is often bare ``dk``.
    activate(parser)

    argv = sys.argv[1:]

    if argv and argv[0] in ("-h", "--help"):
        parser.print_help()
        sys.exit(0)

    try:
        ns = _parse_dk(config, argv, parser)
    except SystemExit as exc:
        if exc.code == 0:
            raise
        envs = list_dk_env_names(config)
        first = argv[0] if argv else ""
        if first and first not in (
            "build",
            "push",
            "clean-images",
            "transfer",
            "fetch",
            "digoc",
            "proxy",
            "cut-release",
        ) and first not in envs:
            print(
                f"dk: unknown command or environment {first!r}. "
                f"Use `dk build`, `dk push`, `dk clean-images`, `dk transfer`, `dk fetch`, "
                f"`dk digoc`, `dk proxy`, `dk cut-release`, or a name with "
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
        raise

    cmd = ns.dk_command

    if cmd == "build":
        sys.exit(_cmd_build(config.compose_prod, argparse.Namespace(services=ns.services), config))
    if cmd == "push":
        sys.exit(_cmd_push(config.compose_prod, ns, config))
    if cmd == "clean-images":
        delete_untagged = None
        if ns.delete_untagged is not None:
            delete_untagged = ns.delete_untagged == "true"
        sys.exit(
            clean_images(
                config,
                apply=ns.apply,
                yes=ns.yes,
                keep_n_tagged=ns.keep_n_tagged,
                older_than=ns.older_than,
                delete_untagged=delete_untagged,
                package=ns.package,
                token=ns.token,
            )
        )
    if cmd == "transfer":
        sys.exit(cmd_transfer(ns, config))
    if cmd == "fetch":
        sys.exit(cmd_fetch(ns, config))
    if cmd == "digoc":
        sys.exit(dispatch_digoc(ns))
    if cmd == "proxy":
        sys.exit(cmd_proxy(ns))
    if cmd == "cut-release":
        sys.exit(
            cut_release(
                repo_root=config.repo_root,
                bump=ns.bump,
                beta=ns.beta,
                beta_w=ns.beta_w,
                submodule=ns.submodule,
                execute=ns.execute,
                yes=ns.yes,
                set_default=ns.set_default,
                tag=ns.tag,
                next_branch=ns.next_branch,
                pin_submodule=ns.pin_submodule,
                image_env=ns.image_env,
                allow_prod_beta=ns.allow_prod_beta,
                allow_dirty=ns.allow_dirty,
            )
        )

    if getattr(ns, "env_name", None):
        sys.exit(handle_env_command(ns, config))

    print("dk: internal error (no handler matched)", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    run_cli(_main_impl, label="dk")
