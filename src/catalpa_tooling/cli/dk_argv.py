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


YES_FLAGS: frozenset[str] = frozenset({"-y", "--yes"})


def _command_words(tokens: list[str]) -> list[str]:
    """The env command and its subcommand from the tokens after the env name.

    Skips the env-level flags (``--dry-run``, ``-y``/``--yes``, ``--tag``) that may come first.
    """
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--dry-run", *YES_FLAGS) or tok.startswith("--tag="):
            i += 1
        elif tok == "--tag":
            i += 2
        else:
            return tokens[i : i + 2]
    return []


def _passes_args_on(words: list[str]) -> bool:
    """True when the command hands its arguments to another program.

    A ``-y`` there may belong to that program (``dk dev manage somecommand -y …``), so only
    a trailing one is taken as dk's. ``host create`` is not one: it parses its own options.
    """
    if not words:
        return True
    first, second = words[0], (words[1] if len(words) > 1 else "")
    if first not in SPECIAL_ENV_COMMANDS or first in ("compose", "docker", "manage"):
        return True
    return first in ("db", "bkp_db") and second == "pgdump"


def normalize_dk_env_argv(argv: list[str]) -> list[str]:
    """Rewrite argv for ``dk <env> …`` (no ``dk`` token).

    With per-env argparse subparsers, ``--tag`` / ``--dry-run`` stay after the env name
    (e.g. ``local --tag v1 info``). This helper only normalizes:

    - ``<env> --help`` / ``-h`` (two args only) → ``--help`` (env CLI help, not compose).
    - ``--yes`` / ``-y`` → ``<env> --yes …``, because only the env parser defines it. For dk's
      own commands that means any ``--yes`` before ``--``; everything after ``--`` is passed on
      untouched (``db restore --yes -- --type=immediate``). For commands that pass their
      arguments to another program (compose verbs, ``compose``, ``docker``, ``manage``,
      ``pgdump``) only a trailing ``--yes`` is moved, so it is not swallowed by REMAINDER.
      Compose verbs typed straight after the env (``dk dev exec …``) lose every ``-y`` anyway,
      in ``_split_env_flags``; see GitHub #164.

    The first two rules follow the POSIX Utility Syntax Guidelines, guideline 10 (``--`` ends
    the options; what follows is operands even if it starts with ``-``,
    https://pubs.opengroup.org/onlinepubs/9799919799/basedefs/V1_chap12.html) and clig.dev
    ("make arguments, flags and subcommands order-independent", https://clig.dev/). argparse
    cannot do the second itself: ``parse_intermixed_args`` does not support subparsers. The
    trailing rule for passthrough commands goes against guideline 10 and is kept because
    scripts rely on it, e.g. ``dk <env> compose down -v --yes`` for the wipe guard.
    """
    out = list(argv)
    if len(out) == 2 and not out[0].startswith("-") and out[1] in ("--help", "-h"):
        return ["--help"]
    if len(out) < 3 or out[0].startswith("-"):
        return out
    if _passes_args_on(_command_words(out[1:])):
        if out[-1] in YES_FLAGS:
            yes_flag = out.pop()
            out.insert(1, yes_flag)
        return out
    end = out.index("--") if "--" in out else len(out)
    head = out[1:end]
    yes_flags = [tok for tok in head if tok in YES_FLAGS]
    if not yes_flags:
        return out
    return [out[0], yes_flags[0], *(tok for tok in head if tok not in YES_FLAGS), *out[end:]]


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
