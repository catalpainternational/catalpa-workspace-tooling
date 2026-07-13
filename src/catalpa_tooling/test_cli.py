"""argparse entrypoint for tests: pytest (backend) and Vitest (frontend)."""

import sys
from pathlib import Path

from catalpa_tooling.cli.completion import activate
from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.deprecation import warn_deprecated
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.smoke_cli import run_smoke
from catalpa_tooling.compliance_cli import run_compliance
from catalpa_tooling.test_parser import build_test_parser


def _config() -> ProjectConfig:
    return ProjectConfig.from_cwd()


def _uv_child_env() -> dict[str, str]:
    import os

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
    parser = build_test_parser(config=_config())
    activate(parser)
    args = parser.parse_args()
    extra = list(getattr(args, "pytest_args", None) or [])

    if args.command == "backend":
        sys.exit(_run_pytest(extra))
    if args.command == "frontend":
        sys.exit(_run_vitest(extra))
    if args.command == "workspace":
        sys.exit(_run_workspace_tests(extra))
    if args.command == "smoke":
        import os

        ci_mode = bool(getattr(args, "ci", False)) or (
            (os.environ.get("CI") or "").strip().lower() in {"1", "true", "yes"}
        )
        smoke_extra = [a for a in extra if a != "--"]
        sys.exit(
            run_smoke(
                _config(),
                env_name=getattr(args, "env", "dev"),
                no_up=bool(getattr(args, "no_up", False)),
                check_only=bool(getattr(args, "check_only", False)),
                fresh_db=bool(getattr(args, "fresh_db", False)),
                ci_mode=ci_mode,
                pytest_args=smoke_extra,
            )
        )
    if args.command == "compliance":
        import os

        ci_mode = bool(getattr(args, "ci", False)) or (
            (os.environ.get("CI") or "").strip().lower() in {"1", "true", "yes"}
        )
        sys.exit(
            run_compliance(
                _config(),
                check_only=bool(getattr(args, "check_only", False)),
                sbom_only=bool(getattr(args, "sbom_only", False)),
                ci_mode=ci_mode,
            )
        )

    sys.exit(1)


def main() -> None:
    entry = Path(sys.argv[0]).name
    if entry == "test":
        warn_deprecated("test", "tests")
    run_cli(_test_main, label="tests")
