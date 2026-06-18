"""Discover project bash scripts under ``paths.scripts`` for ``native`` and ``scripts`` CLIs."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

NATIVE_SCRIPT_PREFIX = "native-"
DEPRECATED_LOCAL_SCRIPT_PREFIX = "local-"
DEPRECATED_DEV_SCRIPT_PREFIX = "dev-"
SCRIPT_SUFFIX = ".sh"
NATIVE_RESET_DB_POST_SCRIPT = "native-reset-db-post.sh"
DEPRECATED_LOCAL_RESET_DB_POST_SCRIPT = "local-reset-db-post.sh"
DEPRECATED_RESET_DB_POST_SCRIPT = "dev-reset-db-post.sh"

RESERVED_NATIVE_COMMANDS: frozenset[str] = frozenset(
    {
        "fetch",
        "runserver",
        "manage",
        "reset-db",
        "pg-restore",
        "frontend",
        "vite",
        "start",
    }
)

_FILENAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*\.sh$")


def _filename_to_command(stem: str) -> str:
    """Map script stem (no ``.sh``) to a CLI subcommand (kebab-case)."""
    return stem.replace("_", "-")


def _command_from_prefixed_script(filename: str, prefix: str) -> str | None:
    if not filename.startswith(prefix) or not filename.endswith(SCRIPT_SUFFIX):
        return None
    if not _FILENAME_RE.match(filename):
        return None
    stem = filename[: -len(SCRIPT_SUFFIX)]
    suffix = stem[len(prefix) :]
    if not suffix:
        return None
    return _filename_to_command(suffix)


def native_command_from_script_name(filename: str) -> str | None:
    return _command_from_prefixed_script(filename, NATIVE_SCRIPT_PREFIX)


def local_command_from_script_name(filename: str) -> str | None:
    return _command_from_prefixed_script(filename, DEPRECATED_LOCAL_SCRIPT_PREFIX)


def dev_command_from_script_name(filename: str) -> str | None:
    return _command_from_prefixed_script(filename, DEPRECATED_DEV_SCRIPT_PREFIX)


def scripts_command_from_script_name(filename: str) -> str | None:
    """Return CLI name for a non-extension ``*.sh`` helper."""
    if (
        filename.startswith(NATIVE_SCRIPT_PREFIX)
        or filename.startswith(DEPRECATED_LOCAL_SCRIPT_PREFIX)
        or filename.startswith(DEPRECATED_DEV_SCRIPT_PREFIX)
        or not filename.endswith(SCRIPT_SUFFIX)
    ):
        return None
    if filename.startswith("."):
        return None
    if not _FILENAME_RE.match(filename):
        return None
    stem = filename[: -len(SCRIPT_SUFFIX)]
    if not stem:
        return None
    return _filename_to_command(stem)


def discover_native_commands(
    scripts_dir: Path,
    *,
    reserved: frozenset[str] = RESERVED_NATIVE_COMMANDS,
) -> dict[str, Path]:
    """Map ``native`` subcommand names to ``scripts/native-*.sh`` (or deprecated prefixes)."""
    if not scripts_dir.is_dir():
        return {}
    found: dict[str, Path] = {}
    deprecated_local: dict[str, Path] = {}
    deprecated_dev: dict[str, Path] = {}
    for path in sorted(scripts_dir.iterdir()):
        if not path.is_file():
            continue
        cmd = native_command_from_script_name(path.name)
        if cmd is not None and cmd not in reserved:
            found[cmd] = path.resolve()
            continue
        cmd = local_command_from_script_name(path.name)
        if cmd is not None and cmd not in reserved:
            deprecated_local[cmd] = path.resolve()
            continue
        cmd = dev_command_from_script_name(path.name)
        if cmd is not None and cmd not in reserved:
            deprecated_dev[cmd] = path.resolve()
    for cmd, path in deprecated_local.items():
        if cmd not in found:
            found[cmd] = path
    for cmd, path in deprecated_dev.items():
        if cmd not in found:
            found[cmd] = path
    return found


def discover_local_commands(
    scripts_dir: Path,
    *,
    reserved: frozenset[str] = RESERVED_NATIVE_COMMANDS,
) -> dict[str, Path]:
    """Backward-compatible alias for ``discover_native_commands``."""
    return discover_native_commands(scripts_dir, reserved=reserved)


def discover_dev_commands(
    scripts_dir: Path,
    *,
    reserved: frozenset[str] = RESERVED_NATIVE_COMMANDS,
) -> dict[str, Path]:
    """Backward-compatible alias for ``discover_native_commands``."""
    return discover_native_commands(scripts_dir, reserved=reserved)


def discover_scripts_commands(scripts_dirs: Path | Sequence[Path]) -> dict[str, Path]:
    """Map ``scripts`` subcommand names to ``*.sh`` under one or more directories.

    When multiple directories are given, earlier entries win on command-name collision.
    Missing directories are skipped.
    """
    dirs: tuple[Path, ...]
    if isinstance(scripts_dirs, Path):
        dirs = (scripts_dirs,)
    else:
        dirs = tuple(scripts_dirs)

    found: dict[str, Path] = {}
    for scripts_dir in dirs:
        if not scripts_dir.is_dir():
            continue
        for path in sorted(scripts_dir.iterdir()):
            if not path.is_file():
                continue
            cmd = scripts_command_from_script_name(path.name)
            if cmd is None or cmd in found:
                continue
            found[cmd] = path.resolve()
    return found


def reset_db_post_script(scripts_dir: Path) -> tuple[Path | None, str | None]:
    """Optional hook run after PostGIS when resetting the host DB.

    Returns ``(path, deprecated_prefix)`` where ``deprecated_prefix`` is
    ``local-``, ``dev-``, or ``None`` for canonical ``native-reset-db-post.sh``.
    """
    native_path = scripts_dir / NATIVE_RESET_DB_POST_SCRIPT
    if native_path.is_file():
        return native_path.resolve(), None
    local_path = scripts_dir / DEPRECATED_LOCAL_RESET_DB_POST_SCRIPT
    if local_path.is_file():
        return local_path.resolve(), "local-"
    dev_path = scripts_dir / DEPRECATED_RESET_DB_POST_SCRIPT
    if dev_path.is_file():
        return dev_path.resolve(), "dev-"
    return None, None
