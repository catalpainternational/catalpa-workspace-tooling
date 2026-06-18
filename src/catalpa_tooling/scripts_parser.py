"""argparse tree for ``scripts``."""

from __future__ import annotations

import argparse

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.script_discovery import discover_scripts_commands


def build_scripts_parser(config: ProjectConfig) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts",
        description=f"Run helper scripts from {config.paths.scripts}/ (*.sh, excluding dev-*.sh).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    discovered = discover_scripts_commands(config.scripts_dir)
    for cmd_name, script_path in discovered.items():
        rel = script_path.relative_to(config.repo_root)
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
    return parser
