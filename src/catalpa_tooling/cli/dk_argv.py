"""Normalize ``dk`` argv before argparse (legacy flag ordering)."""

from __future__ import annotations

import argparse
from typing import Any

SPECIAL_ENV_COMMANDS: frozenset[str] = frozenset(
    {
        "info",
        "secrets",
        "host",
        "zabbix",
        "ensure_volumes",
        "storage",
        "trust-caddy-cert",
        "manage",
        "pull_media",
        "wipe",
        "files",
        "bkp_files",
        "db",
        "bkp_db",
        "dc-backup",
        "compose",
    }
)


def normalize_dk_root_argv(argv: list[str]) -> list[str]:
    """Rewrite top-level argv for ``dk`` before the root parser runs.

    - ``--dry-run <env> …`` → ``<env> --dry-run …``
    """
    out = list(argv)
    if len(out) >= 2 and out[0] == "--dry-run" and not out[1].startswith("-"):
        out = [out[1], "--dry-run", *out[2:]]
    return out


def normalize_dk_env_argv(argv: list[str]) -> list[str]:
    """Rewrite argv for ``dk <env> …`` (no ``dk`` token).

    With per-env argparse subparsers, ``--tag`` / ``--dry-run`` stay after the env name
    (e.g. ``local --tag v1 info``). This helper only normalizes:

    - ``<env> --help`` / ``-h`` (two args only) → ``--help`` (env CLI help, not compose).
    - Trailing ``--yes`` / ``-y`` → ``<env> --yes …`` so confirmation flags are not swallowed
      by ``implicit_compose_argv`` (REMAINDER).
    """
    out = list(argv)
    if len(out) == 2 and not out[0].startswith("-") and out[1] in ("--help", "-h"):
        return ["--help"]
    if len(out) >= 3 and not out[0].startswith("-") and out[-1] in ("--yes", "-y"):
        yes_flag = out.pop()
        out.insert(1, yes_flag)
    return out


def _split_env_flags(tokens: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Peel ``--dry-run``, ``-y``/``--yes``, and ``--tag`` from tokens after env name."""
    dry_run = False
    yes = False
    tag: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--dry-run":
            dry_run = True
            i += 1
            continue
        if tok in ("-y", "--yes"):
            yes = True
            i += 1
            continue
        if tok == "--tag" and i + 1 < len(tokens):
            tag = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("--tag="):
            tag = tok.split("=", 1)[1] or None
            i += 1
            continue
        rest.append(tok)
        i += 1
    return {"dry_run": dry_run, "yes": yes, "tag": tag}, rest


def is_implicit_compose_argv(argv: list[str]) -> bool:
    """True when ``argv`` (no ``dk`` token) should use implicit compose passthrough."""
    if not argv or argv[0].startswith("-"):
        return False
    if len(argv) == 1:
        return True
    normalized = normalize_dk_env_argv(argv)
    if normalized == ["--help"]:
        return False
    _, rest = _split_env_flags(normalized[1:])
    if not rest:
        return True
    first = rest[0]
    if first not in SPECIAL_ENV_COMMANDS:
        return True
    if first == "host" and len(rest) >= 2 and rest[1] == "create":
        return False
    if first == "db" and len(rest) >= 2 and rest[1] == "configure":
        return False
    if first == "bkp_db" and len(rest) >= 2 and rest[1] == "configure":
        return False
    return False


def build_implicit_compose_namespace(argv: list[str]) -> argparse.Namespace:
    """Build namespace for ``dk <env> up -d`` style implicit compose passthrough."""
    normalized = normalize_dk_env_argv(argv)
    env_name = normalized[0]
    flags, rest = _split_env_flags(normalized[1:])
    return argparse.Namespace(
        dk_command=env_name,
        env_name=env_name,
        env_command=None,
        implicit_compose_argv=rest,
        compose_argv=[],
        dry_run=flags["dry_run"],
        yes=flags["yes"],
        tag=flags["tag"],
    )
