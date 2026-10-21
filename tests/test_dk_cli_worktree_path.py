"""#104: `dk --worktree <slug>` from inside another worktree must not crash on `Path`."""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalpa_tooling import dk_cli


class TestWorktreeRelativePathFallback(unittest.TestCase):
    """The `except ValueError` branch used `Path` without importing it (NameError)."""

    def test_path_is_importable_from_the_module(self) -> None:
        """The regression itself: the name has to resolve at module scope."""
        self.assertIs(dk_cli.Path, Path)

    def test_worktree_outside_cwd_root_uses_the_fallback_label(self) -> None:
        """cwd is already a worktree, so wt_root is not relative to it — the crashing path."""
        cwd_root = Path("/repo/.worktrees/other")
        wt_root = Path("/repo/.worktrees/target")

        with (
            patch.object(dk_cli, "peel_worktree_flag", return_value=("target", ["dev"])),
            patch.object(dk_cli, "repo_root_from_cwd", return_value=cwd_root),
            patch.object(dk_cli, "resolve_worktree_root", return_value=wt_root),
            patch.object(dk_cli, "load_project_config", return_value=MagicMock()),
            patch.object(dk_cli.os, "chdir"),
            patch("sys.stderr"),
        ):
            _, rest, slug = dk_cli._load_config_for_argv(["--worktree", "target", "dev"])

        self.assertEqual(rest, ["dev"])
        self.assertEqual(slug, "target")


if __name__ == "__main__":
    unittest.main()
