"""argparse tree for ``tests``."""

from __future__ import annotations

import argparse

from catalpa_tooling.config import ProjectConfig


def build_test_parser(*, config: ProjectConfig | None = None) -> argparse.ArgumentParser:
    _ = config
    parser = argparse.ArgumentParser(
        prog="tests",
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
    p_smoke = subparsers.add_parser(
        "smoke",
        help="Project smoke tests (stack, migrations, HTTP, Playwright PWA load).",
    )
    p_smoke.add_argument("--env", default="dev", help="Deploy env (default: dev).")
    p_smoke.add_argument("--no-up", action="store_true", help="Skip docker compose up -d.")
    p_smoke.add_argument(
        "--check-only",
        action="store_true",
        help="Use migrate --check instead of migrate --noinput.",
    )
    p_smoke.add_argument(
        "--fresh-db",
        action="store_true",
        help="Ephemeral empty-DB migration test (local; skipped in CI).",
    )
    p_smoke.add_argument("--ci", action="store_true", help="CI mode (ignore --fresh-db).")
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
