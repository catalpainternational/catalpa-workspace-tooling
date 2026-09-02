"""Package-manager command construction for the frontend (npm / yarn / pnpm).

Shared by ``native frontend`` and ``tests frontend`` so both honour
``native.frontend.package_manager`` (and its auto-detection) identically.
"""

from __future__ import annotations


def package_install_cmd(package_manager: str) -> list[str]:
    """Dependency-install argv for ``package_manager``."""
    if package_manager == "yarn":
        return ["yarn", "install"]
    if package_manager == "pnpm":
        return ["pnpm", "install"]
    return ["npm", "install"]


def package_run_cmd(package_manager: str, script: str, *, args: list[str] | None = None) -> list[str]:
    """Argv running ``script`` from package.json, forwarding ``args`` to the script.

    npm and yarn need an explicit ``--`` separator before script arguments; pnpm forwards
    trailing arguments directly and treats a bare ``--`` as an argument in newer versions.
    """
    cmd = [package_manager, "run", script]
    if not args:
        return cmd
    if package_manager == "pnpm":
        return [*cmd, *args]
    return [*cmd, "--", *args]
