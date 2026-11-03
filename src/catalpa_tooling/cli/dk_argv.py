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
        "docker",
    }
)

# Docker Compose verbs that ``dk <env> <verb> …`` forwards to compose.
#
# A first token that is neither one of these nor a ``SPECIAL_ENV_COMMANDS`` entry is a typo,
# not passthrough. It used to fall through to implicit compose anyway, and an *empty*
# passthrough defaults to ``up -d`` — so mistyping a subcommand started or recreated the
# stack instead of reporting the mistake (#85). ``dk dev context`` was the reported case:
# ``context`` is a ``dk worktree`` subcommand, and typing it against an env brought the
# stack up.
#
# Deliberately generous: every verb compose accepts belongs here, because the cost of
# omitting one is refusing a command that used to work.
COMPOSE_VERBS: frozenset[str] = frozenset(
    {
        "attach",
        "build",
        "config",
        "cp",
        "create",
        "down",
        "events",
        "exec",
        "images",
        "kill",
        "logs",
        "ls",
        "pause",
        "port",
        "ps",
        "pull",
        "push",
        "restart",
        "rm",
        "run",
        "scale",
        "start",
        "stats",
        "stop",
        "top",
        "unpause",
        "up",
        "version",
        "wait",
        "watch",
    }
)


def peel_worktree_flag(argv: list[str]) -> tuple[str | None, list[str]]:
    """Strip ``--worktree SLUG`` / ``-W SLUG`` / ``--worktree=SLUG`` from argv.

    Returns ``(slug_or_none, remaining_argv)``. Does not validate the slug path.
    Raises ``ValueError`` when the flag is present without a slug or given twice.
    """
    out: list[str] = []
    slug: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--worktree", "-W"):
            if i + 1 >= len(argv) or argv[i + 1].startswith("-"):
                raise ValueError(
                    f"dk: {tok} requires a worktree slug "
                    "(e.g. dk --worktree onboarding dev up -d)"
                )
            if slug is not None:
                raise ValueError("dk: --worktree / -W may only be given once")
            slug = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--worktree="):
            if slug is not None:
                raise ValueError("dk: --worktree / -W may only be given once")
            slug = tok.split("=", 1)[1]
            if not slug.strip():
                raise ValueError(
                    "dk: --worktree= requires a worktree slug "
                    "(e.g. dk --worktree=onboarding dev up -d)"
                )
            i += 1
            continue
        out.append(tok)
        i += 1
    return (slug.strip() if slug else None), out


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
        # Only a real compose verb is passthrough; anything else falls through to argparse,
        # which reports the mistake rather than silently running `up -d` (#85).
        return first in COMPOSE_VERBS
    if first == "host" and len(rest) >= 2 and rest[1] == "create":
        return False
    if first == "db" and len(rest) >= 2 and rest[1] == "configure":
        return False
    if first == "bkp_db" and len(rest) >= 2 and rest[1] == "configure":
        return False
    return False


def unknown_env_subcommand(argv: list[str]) -> str | None:
    """First token of ``dk <env> …`` when it is neither a dk subcommand nor a compose verb.

    ``None`` when the argv is fine. Used to report the mistake with both sets of valid names:
    argparse alone lists only its own subparser choices, so a user who mistyped would be shown
    a list with no ``up`` or ``logs`` in it and could reasonably conclude those were invalid
    too (#85).
    """
    if not argv or argv[0].startswith("-") or len(argv) == 1:
        return None
    normalized = normalize_dk_env_argv(argv)
    if normalized == ["--help"]:
        return None
    _, rest = _split_env_flags(normalized[1:])
    if not rest:
        return None
    first = rest[0]
    if first.startswith("-"):
        return None
    if first in SPECIAL_ENV_COMMANDS or first in COMPOSE_VERBS:
        return None
    return first


def unknown_env_subcommand_message(env_name: str, token: str) -> str:
    """Error text naming both the dk subcommands and the compose verbs that are valid."""
    dk_names = ", ".join(sorted(SPECIAL_ENV_COMMANDS))
    compose_names = ", ".join(sorted(COMPOSE_VERBS))
    return (
        f"dk {env_name}: unknown subcommand {token!r}.\n"
        f"  dk subcommands: {dk_names}\n"
        f"  compose verbs:  {compose_names}\n"
        f"  Bare `dk {env_name}` runs `up -d`, so an unrecognised subcommand is refused here\n"
        "  rather than starting the stack."
    )


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
