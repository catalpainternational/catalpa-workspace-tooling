"""argparse tree for ``tests``."""

from __future__ import annotations

import argparse

from catalpa_tooling.config import ProjectConfig

# Default Playwright slow-mo for ``tests functional headed`` (milliseconds).
FUNCTIONAL_HEADED_SLOWMO_MS = 250


def _add_stack_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env", default="dev", help="Deploy env (default: dev).")
    parser.add_argument("--no-up", action="store_true", help="Skip docker compose up -d.")


def _add_ci_gate_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Use migrate --check instead of migrate --noinput.",
    )
    parser.add_argument(
        "--fresh-db",
        action="store_true",
        help="Ephemeral empty-DB migrate (default for local CI gate; ignored in CI).",
    )
    parser.add_argument(
        "--no-fresh-db",
        action="store_true",
        help="Skip ephemeral empty-DB migrate (still migrates primary DB).",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: assume primary DB is empty; skip ephemeral fresh-db.",
    )


def build_test_parser(*, config: ProjectConfig | None = None) -> argparse.ArgumentParser:
    _ = config
    parser = argparse.ArgumentParser(
        prog="tests",
        description="Run backend pytest, frontend Vitest, CI gate, or functional Playwright.",
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

    p_ci = subparsers.add_parser(
        "ci",
        help=(
            "CI gate: empty migrate, manage check, makemigrations --check, "
            "frontend type-check/build, guest Playwright."
        ),
    )
    _add_stack_flags(p_ci)
    _add_ci_gate_flags(p_ci)
    p_ci.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Extra pytest args after `--` (guest suite; elearning stays skipped).",
    )

    p_functional = subparsers.add_parser(
        "functional",
        help=(
            "Functional Playwright against a running stack (skips CI gate). "
            "Use `tests functional headed` for a visible browser "
            f"(default slow-mo {FUNCTIONAL_HEADED_SLOWMO_MS} ms)."
        ),
    )
    _add_stack_flags(p_functional)
    p_functional.add_argument(
        "--headed",
        action="store_true",
        help=f"Visible browser with default slow-mo ({FUNCTIONAL_HEADED_SLOWMO_MS} ms).",
    )
    p_functional.add_argument(
        "rest",
        nargs=argparse.REMAINDER,
        help="Optional `headed`, then pytest args (after `--` if needed).",
    )

    # Deprecated alias — prefer ``tests ci`` / ``tests functional``.
    p_smoke = subparsers.add_parser(
        "smoke",
        help="Deprecated alias for `tests ci` (or `tests functional` with --functional).",
    )
    _add_stack_flags(p_smoke)
    _add_ci_gate_flags(p_smoke)
    p_smoke.add_argument(
        "--functional",
        action="store_true",
        help="Deprecated: use `tests functional` instead.",
    )
    p_smoke.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Extra pytest args after `--`.",
    )

    p_compliance = subparsers.add_parser(
        "compliance",
        help="OSS compliance scan (licenses, SBOM, NOTICES, policy gate).",
    )
    p_compliance.add_argument(
        "--check-only",
        action="store_true",
        help="Fail on policy violations or stale committed artifacts (no writes).",
    )
    p_compliance.add_argument(
        "--sbom-only",
        action="store_true",
        help="Regenerate CycloneDX SBOM files only (skip metadata and policy checks).",
    )
    p_compliance.add_argument(
        "--ci",
        action="store_true",
        help="CI mode (non-interactive).",
    )
    return parser
