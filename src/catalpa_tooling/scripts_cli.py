"""argparse entrypoint for scripts: run bash helpers under paths.scripts from tooling.yaml."""

import argparse
import sys
from pathlib import Path

from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.run_cmd import run as run_cmd


def _config() -> ProjectConfig:
    return ProjectConfig.from_cwd()


def _run_script(name: str, extra: list[str]) -> int:
    cfg = _config()
    script = cfg.scripts_dir / name
    if not script.is_file():
        print(f"Missing {script}", file=sys.stderr)
        return 1
    cmd = ["bash", str(script), *extra]
    return run_cmd(cmd, cwd=cfg.repo_root, check=False).returncode


def _run_merge_tetum_po(source_dir: Path | None) -> int:
    cfg = _config()
    root = cfg.repo_root
    default_src = cfg.backend_dir / "wagtail_translations"
    src = source_dir if source_dir is not None else default_src
    if not src.is_dir():
        print(f"Not a directory: {src}", file=sys.stderr)
        return 1
    locale_root = cfg.backend_dir / "locale"
    return _run_script(
        "merge_tetum_po.sh",
        [str(src.resolve()), str(locale_root.resolve())],
    )


def _scripts_main() -> None:
    cfg = _config()
    parser = argparse.ArgumentParser(
        prog="scripts",
        description=f"Run helper scripts from {cfg.paths.scripts}/",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "fetch-db",
        help="Fetch database dump (fetch_db.sh).",
    ).set_defaults(handler="fetch-db")
    subparsers.add_parser(
        "fetch-media",
        help="Fetch media via rsync (fetch_media.sh).",
    ).set_defaults(handler="fetch-media")
    subparsers.add_parser(
        "trust-caddy-cert",
        help="Trust local Caddy CA (trust-caddy-cert.sh).",
    ).set_defaults(handler="trust-caddy-cert")

    p_merge = subparsers.add_parser(
        "merge-tetum-po",
        help="Merge Tetum .po catalogs into backend locale (gettext msgcat).",
    )
    p_merge.add_argument(
        "--source",
        type=Path,
        default=None,
        help=f"Directory of source .po files (default: {cfg.paths.backend}/wagtail_translations).",
    )
    p_merge.set_defaults(handler="merge-tetum-po")

    args = parser.parse_args()
    handler = args.handler

    if handler == "fetch-db":
        sys.exit(_run_script("fetch_db.sh", []))
    if handler == "fetch-media":
        sys.exit(_run_script("fetch_media.sh", []))
    if handler == "trust-caddy-cert":
        sys.exit(_run_script("trust-caddy-cert.sh", []))
    if handler == "merge-tetum-po":
        sys.exit(_run_merge_tetum_po(getattr(args, "source", None)))

    sys.exit(1)


def main() -> None:
    run_cli(_scripts_main, label="scripts")
