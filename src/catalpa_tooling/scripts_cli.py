"""argparse entrypoint for scripts: run bash helpers under paths.scripts from tooling.yaml."""

import argparse
import sys

from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.script_discovery import discover_scripts_commands
from catalpa_tooling.script_runner import run_bash_script


def _config() -> ProjectConfig:
    return ProjectConfig.from_cwd()


def _scripts_main() -> None:
    cfg = _config()
    parser = argparse.ArgumentParser(
        prog="scripts",
        description=f"Run helper scripts from {cfg.paths.scripts}/ (*.sh, excluding dev-*.sh).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discovered = discover_scripts_commands(cfg.scripts_dir)
    for cmd_name, script_path in discovered.items():
        rel = script_path.relative_to(cfg.repo_root)
        p = subparsers.add_parser(
            cmd_name,
            help=f"Run {rel}.",
        )
        p.add_argument(
            "script_args",
            nargs=argparse.REMAINDER,
            help=f"Arguments forwarded to {script_path.name}.",
        )
        p.set_defaults(script_path=script_path)

    args = parser.parse_args()
    script_path = getattr(args, "script_path", None)
    if script_path is None:
        parser.error("unknown command")
    extra = [a for a in getattr(args, "script_args", []) if a]
    sys.exit(run_bash_script(cfg, script_path, extra, label=f"scripts {args.command}"))


def main() -> None:
    run_cli(_scripts_main, label="scripts")
