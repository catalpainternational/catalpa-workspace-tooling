"""Package-manager argv construction shared by ``native frontend`` and ``tests frontend``."""

from __future__ import annotations

import pytest

from catalpa_tooling.frontend_pkg import package_install_cmd, package_run_cmd


@pytest.mark.parametrize(
    ("package_manager", "expected"),
    [
        ("npm", ["npm", "install"]),
        ("yarn", ["yarn", "install"]),
        ("pnpm", ["pnpm", "install"]),
        ("unknown", ["npm", "install"]),
    ],
)
def test_install_cmd(package_manager: str, expected: list[str]) -> None:
    assert package_install_cmd(package_manager) == expected


@pytest.mark.parametrize("package_manager", ["npm", "yarn", "pnpm"])
def test_run_cmd_without_args_omits_separator(package_manager: str) -> None:
    assert package_run_cmd(package_manager, "test") == [package_manager, "run", "test"]


@pytest.mark.parametrize(
    ("package_manager", "expected"),
    [
        ("npm", ["npm", "run", "test", "--", "-t", "foo"]),
        ("yarn", ["yarn", "run", "test", "--", "-t", "foo"]),
        # pnpm forwards trailing args directly; a literal `--` would be passed to the script.
        ("pnpm", ["pnpm", "run", "test", "-t", "foo"]),
    ],
)
def test_run_cmd_forwards_args(package_manager: str, expected: list[str]) -> None:
    assert package_run_cmd(package_manager, "test", args=["-t", "foo"]) == expected
