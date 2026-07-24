"""argparse entrypoint for tests: pytest (backend), Vitest, CI gate, functional Playwright."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from catalpa_tooling.cli.completion import activate
from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.deprecation import warn_deprecated
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.smoke_cli import (
    functional_pytest_args,
    run_smoke,
)
from catalpa_tooling.compliance_cli import run_compliance
from catalpa_tooling.test_parser import FUNCTIONAL_HEADED_SLOWMO_MS, build_test_parser


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


def _ci_env_mode(explicit: bool = False) -> bool:
    return explicit or ((os.environ.get("CI") or "").strip().lower() in {"1", "true", "yes"})


def _strip_pytest_sep(extra: list[str]) -> list[str]:
    return [a for a in extra if a != "--"]


def parse_functional_rest(
    rest: list[str],
    *,
    headed_flag: bool = False,
    no_up_flag: bool = False,
    env_flag: str = "dev",
) -> tuple[bool, bool, str, list[str]]:
    """Interpret ``tests functional`` remainer: optional ``headed``, stack flags, pytest args.

    Supports ``tests functional headed --no-up -- -k lesson`` and
    ``tests functional --no-up headed``.
    """
    tokens = list(rest or [])
    headed = headed_flag
    no_up = no_up_flag
    env_name = env_flag
    pytest_args: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            pytest_args.extend(tokens[i + 1 :])
            break
        if tok == "headed" and not pytest_args:
            headed = True
            i += 1
            continue
        if tok == "--no-up":
            no_up = True
            i += 1
            continue
        if tok == "--headed":
            headed = True
            i += 1
            continue
        if tok == "--env" and i + 1 < len(tokens):
            env_name = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--env="):
            env_name = tok.split("=", 1)[1]
            i += 1
            continue
        pytest_args.append(tok)
        i += 1
    return headed, no_up, env_name, _strip_pytest_sep(pytest_args)


def _run_ci_gate(args: object) -> int:
    extra = _strip_pytest_sep(list(getattr(args, "pytest_args", None) or []))
    return run_smoke(
        _config(),
        env_name=getattr(args, "env", "dev"),
        no_up=bool(getattr(args, "no_up", False)),
        check_only=bool(getattr(args, "check_only", False)),
        fresh_db=bool(getattr(args, "fresh_db", False)),
        no_fresh_db=bool(getattr(args, "no_fresh_db", False)),
        functional=False,
        ci_mode=_ci_env_mode(bool(getattr(args, "ci", False))),
        pytest_args=extra,
        log_prefix="ci",
    )


def _run_functional(args: object) -> int:
    headed, no_up, env_name, extra = parse_functional_rest(
        list(getattr(args, "rest", None) or getattr(args, "pytest_args", None) or []),
        headed_flag=bool(getattr(args, "headed", False))
        or getattr(args, "mode", None) == "headed",
        no_up_flag=bool(getattr(args, "no_up", False)),
        env_flag=str(getattr(args, "env", "dev") or "dev"),
    )
    pytest_args = functional_pytest_args(
        headed=headed,
        extra=extra,
        slowmo_ms=FUNCTIONAL_HEADED_SLOWMO_MS,
    )
    return run_smoke(
        _config(),
        env_name=env_name,
        no_up=no_up,
        functional=True,
        ci_mode=False,
        pytest_args=pytest_args,
        log_prefix="functional",
    )


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
    if args.command == "ci":
        sys.exit(_run_ci_gate(args))
    if args.command == "functional":
        sys.exit(_run_functional(args))
    if args.command == "smoke":
        warn_deprecated("tests smoke", "tests ci (or tests functional)")
        if bool(getattr(args, "functional", False)):
            # Map legacy ``tests smoke --functional`` → functional path.
            class _LegacyFunctional:
                env = getattr(args, "env", "dev")
                no_up = bool(getattr(args, "no_up", False))
                mode = None
                pytest_args = getattr(args, "pytest_args", None)

            sys.exit(_run_functional(_LegacyFunctional()))
        sys.exit(_run_ci_gate(args))
    if args.command == "compliance":
        ci_mode = _ci_env_mode(bool(getattr(args, "ci", False)))
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
