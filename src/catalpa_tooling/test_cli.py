"""argparse entrypoint for test: pytest (backend) and Vitest (frontend)."""

import argparse
import os
import sys
from pathlib import Path

from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.run_cmd import run as run_cmd


def _config() -> ProjectConfig:
    return ProjectConfig.from_cwd()


def _uv_child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    return env


def _run_pytest(extra: list[str]) -> int:
    cfg = _config()
    cmd = ["uv", "run", "--group", "test", "pytest", *extra]
    return run_cmd(cmd, cwd=cfg.backend_dir, env=_uv_child_env(), check=False).returncode


def _run_vitest(extra: list[str]) -> int:
    cfg = _config()
    cwd = cfg.frontend_dir
    nvm = Path.home() / ".nvm" / "nvm.sh"
    if nvm.is_file() and (cwd / ".nvmrc").is_file():
        quoted = " ".join(extra)
        inner = f"source {nvm} && nvm use && npm run test -- {quoted}".strip()
        cmd = ["bash", "-lc", inner]
    else:
        cmd = ["npm", "run", "test", "--", *extra]
    return run_cmd(cmd, cwd=cwd, env=_uv_child_env(), check=False).returncode


def _run_workspace_tests(extra: list[str]) -> int:
    cfg = _config()
    cmd = ["uv", "run", "--group", "test", "pytest", *extra]
    return run_cmd(cmd, cwd=cfg.repo_root, env=_uv_child_env(), check=False).returncode


def _test_main() -> None:
    parser = argparse.ArgumentParser(
        prog="test",
        description="Run backend pytest, frontend Vitest, or repo-root workspace tests.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "backend",
        help="pytest in paths.backend (`uv run --group test pytest`).",
    )
    subparsers.add_parser(
        "frontend",
        help="Vitest in paths.frontend (`npm run test`, with `nvm use` when .nvmrc is present).",
    )
    p_workspace = subparsers.add_parser(
        "workspace",
        help="pytest at repo root (library / tooling tests).",
    )
    p_workspace.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to pytest (e.g. tests/test_foo.py -k name).",
    )

    args = parser.parse_args()
    extra = list(getattr(args, "pytest_args", None) or [])

    if args.command == "backend":
        sys.exit(_run_pytest(extra))
    if args.command == "frontend":
        sys.exit(_run_vitest(extra))
    if args.command == "workspace":
        sys.exit(_run_workspace_tests(extra))

    sys.exit(1)


def main() -> None:
    run_cli(_test_main, label="test")
