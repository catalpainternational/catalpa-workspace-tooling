"""argparse entrypoint for scripts: run bash helpers under paths.scripts from tooling.yaml."""

import sys

from catalpa_tooling.cli.completion import activate
from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.scripts_parser import build_scripts_parser
from catalpa_tooling.script_runner import run_bash_script


def _config() -> ProjectConfig:
    return ProjectConfig.from_cwd()


def _scripts_main() -> None:
    cfg = _config()
    parser = build_scripts_parser(cfg)
    activate(parser)
    args = parser.parse_args()
    script_path = getattr(args, "script_path", None)
    if script_path is None:
        parser.error("unknown command")
    extra = [a for a in getattr(args, "script_args", []) if a]
    sys.exit(run_bash_script(cfg, script_path, extra, label=f"scripts {args.command}"))


def main() -> None:
    run_cli(_scripts_main, label="scripts")
