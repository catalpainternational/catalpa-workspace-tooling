"""Tests for run_cmd helpers."""

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from catalpa_tooling.run_cmd import run_interruptible, terminate_process_tree


class TestTerminateProcessTree(unittest.TestCase):
    def test_noop_when_already_exited(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = 0
        terminate_process_tree(proc)
        proc.terminate.assert_not_called()


class TestRunInterruptible(unittest.TestCase):
    def test_returns_child_exit_code(self) -> None:
        proc = MagicMock()
        proc.wait.return_value = 0
        with patch("catalpa_tooling.run_cmd.subprocess.Popen", return_value=proc):
            r = run_interruptible(["echo", "ok"], print_cmd=False)
        self.assertEqual(r.returncode, 0)

    def test_keyboard_interrupt_terminates_tree_and_runs_cleanup(self) -> None:
        proc = MagicMock()
        proc.wait.side_effect = KeyboardInterrupt
        cleanup = MagicMock()
        with (
            patch("catalpa_tooling.run_cmd.subprocess.Popen", return_value=proc),
            patch("catalpa_tooling.run_cmd.terminate_process_tree") as term,
        ):
            r = run_interruptible(["sleep", "999"], print_cmd=False, on_interrupt=cleanup)
        self.assertEqual(r.returncode, 130)
        term.assert_called_once_with(proc, timeout=30)
        cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
