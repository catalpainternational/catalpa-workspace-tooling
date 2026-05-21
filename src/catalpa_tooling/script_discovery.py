"""Discover project bash scripts under ``paths.scripts`` for ``dev`` and ``scripts`` CLIs."""

from __future__ import annotations

import re
from pathlib import Path

DEV_SCRIPT_PREFIX = "dev-"
DEV_SCRIPT_SUFFIX = ".sh"
RESET_DB_POST_SCRIPT = "dev-reset-db-post.sh"

# Built-in ``dev`` subcommands; discovered ``dev-*.sh`` names must not override these.
RESERVED_DEV_COMMANDS: frozenset[str] = frozenset(
    {
        "fetch",
        "runserver",
        "manage",
        "reset-db",
        "pg-restore",
        "vite",
    }
)

_FILENAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*\.sh$")


def _filename_to_command(stem: str) -> str:
    """Map script stem (no ``.sh``) to a CLI subcommand (kebab-case)."""
    return stem.replace("_", "-")


def dev_command_from_script_name(filename: str) -> str | None:
    """Return CLI name for ``dev-<name>.sh``, or ``None`` if not a dev extension script."""
    if not filename.startswith(DEV_SCRIPT_PREFIX) or not filename.endswith(DEV_SCRIPT_SUFFIX):
        return None
    if not _FILENAME_RE.match(filename):
        return None
    stem = filename[: -len(DEV_SCRIPT_SUFFIX)]
    suffix = stem[len(DEV_SCRIPT_PREFIX) :]
    if not suffix:
        return None
    return _filename_to_command(suffix)


def scripts_command_from_script_name(filename: str) -> str | None:
    """Return CLI name for a non-dev ``*.sh`` helper."""
    if filename.startswith(DEV_SCRIPT_PREFIX) or not filename.endswith(DEV_SCRIPT_SUFFIX):
        return None
    if filename.startswith("."):
        return None
    if not _FILENAME_RE.match(filename):
        return None
    stem = filename[: -len(DEV_SCRIPT_SUFFIX)]
    if not stem:
        return None
    return _filename_to_command(stem)


def discover_dev_commands(
    scripts_dir: Path,
    *,
    reserved: frozenset[str] = RESERVED_DEV_COMMANDS,
) -> dict[str, Path]:
    """Map ``dev`` subcommand names to ``scripts/dev-*.sh`` paths (sorted, no reserved clashes)."""
    if not scripts_dir.is_dir():
        return {}
    found: dict[str, Path] = {}
    for path in sorted(scripts_dir.iterdir()):
        if not path.is_file():
            continue
        cmd = dev_command_from_script_name(path.name)
        if cmd is None or cmd in reserved:
            continue
        found[cmd] = path.resolve()
    return found


def discover_scripts_commands(scripts_dir: Path) -> dict[str, Path]:
    """Map ``scripts`` subcommand names to ``scripts/*.sh`` except ``dev-*.sh``."""
    if not scripts_dir.is_dir():
        return {}
    found: dict[str, Path] = {}
    for path in sorted(scripts_dir.iterdir()):
        if not path.is_file():
            continue
        cmd = scripts_command_from_script_name(path.name)
        if cmd is None:
            continue
        found[cmd] = path.resolve()
    return found


def reset_db_post_script(scripts_dir: Path) -> Path | None:
    """Optional hook run after PostGIS when resetting the local DB."""
    path = scripts_dir / RESET_DB_POST_SCRIPT
    return path.resolve() if path.is_file() else None
