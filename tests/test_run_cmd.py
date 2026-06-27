"""Tests for run_cmd helpers."""

import signal
import subprocess
import threading
import time
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
        proc.poll.side_effect = [None, 0]
        proc.returncode = 0
        proc.wait.return_value = 0
        with patch("catalpa_tooling.run_cmd.subprocess.Popen", return_value=proc):
            r = run_interruptible(["echo", "ok"], print_cmd=False)
        self.assertEqual(r.returncode, 0)

    def test_sigint_returns_130_without_waiting_for_child_exit(self) -> None:
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd=["sleep"], timeout=0.2)
        cleanup = MagicMock()
        handlers: dict[int, object] = {}

        def capture_signal(signum, handler):
            handlers[signum] = handler
            return signal.SIG_DFL

        with (
            patch("catalpa_tooling.run_cmd.subprocess.Popen", return_value=proc),
            patch("catalpa_tooling.run_cmd.terminate_process_tree") as term,
            patch("catalpa_tooling.run_cmd.signal.signal", side_effect=capture_signal),
        ):
            result: list[subprocess.CompletedProcess[str]] = []

            def worker() -> None:
                result.append(
                    run_interruptible(["sleep", "999"], print_cmd=False, on_interrupt=cleanup)
                )

            th = threading.Thread(target=worker, daemon=True)
            th.start()
            deadline = time.monotonic() + 2.0
            while signal.SIGINT not in handlers and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn(signal.SIGINT, handlers)
            handlers[signal.SIGINT](signal.SIGINT, None)  # type: ignore[operator]
            th.join(timeout=3)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].returncode, 130)
        cleanup.assert_called_once()
        term.assert_called_once_with(proc, timeout=30)


if __name__ == "__main__":
    unittest.main()
