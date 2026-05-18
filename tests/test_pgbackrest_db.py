"""Tests for pgbackrest_db helpers."""

import sys
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

from catalpa_tooling.pgbackrest_db import (
    _log_level_argv_shell,
    _restore_db_logs_silenced,
    _restore_recovery_timeout_sec,
    run_drop_create_app_database,
    wait_db_logs_for_recovery_ready,
)


class TestDropCreateAppDatabase(unittest.TestCase):
    def test_invokes_dropdb_createdb_in_container(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("catalpa_tooling.pgbackrest_db.run_cmd", side_effect=fake_run):
            rc = run_drop_create_app_database("compose.yml", {})
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][:9],
            ["docker", "compose", "-f", "compose.yml", "exec", "-T", "db", "sh", "-c"],
        )
        script = calls[0][-1]
        self.assertIn("dropdb", script)
        self.assertIn("--force", script)
        self.assertIn("createdb", script)
        self.assertIn("-O", script)
        self.assertIn('"$DJANGO_APP_DB_USER"', script)


class TestLogLevelArgvShell(unittest.TestCase):
    def test_restore_default_console_info(self) -> None:
        s = _log_level_argv_shell({}, default_console_level="info")
        self.assertIn("--log-level-console=info", s)

    def test_env_overrides_default_console(self) -> None:
        s = _log_level_argv_shell(
            {"PGBR_LOG_LEVEL_CONSOLE": "warn"},
            default_console_level="info",
        )
        self.assertIn("--log-level-console=warn", s)

    def test_no_default_when_unset(self) -> None:
        self.assertEqual(_log_level_argv_shell({}), "")


class TestRestoreRecoveryTimeout(unittest.TestCase):
    def test_default(self) -> None:
        self.assertEqual(_restore_recovery_timeout_sec({}), 3600)

    def test_env_override(self) -> None:
        self.assertEqual(
            _restore_recovery_timeout_sec({"PGBR_RESTORE_RECOVERY_TIMEOUT_SEC": "120"}),
            120,
        )

    def test_minimum(self) -> None:
        self.assertEqual(
            _restore_recovery_timeout_sec({"PGBR_RESTORE_RECOVERY_TIMEOUT_SEC": "10"}),
            30,
        )


class TestRestoreDbLogsSilenced(unittest.TestCase):
    def test_default_not_silenced(self) -> None:
        self.assertFalse(_restore_db_logs_silenced({}))
        self.assertFalse(_restore_db_logs_silenced({"PGBR_RESTORE_SILENCE_DB_LOGS": ""}))

    def test_truthy_values(self) -> None:
        for v in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(
                _restore_db_logs_silenced({"PGBR_RESTORE_SILENCE_DB_LOGS": v}),
                repr(v),
            )


class TestWaitDbLogsForRecoveryReady(unittest.TestCase):
    def test_success(self) -> None:
        proc = MagicMock()
        proc.stdout = StringIO(
            "2026-01-01 00:00:00 UTC LOG:  database system is ready to accept connections\n"
        )
        proc.poll.return_value = None
        proc.wait.return_value = 0
        with (
            patch("catalpa_tooling.pgbackrest_db.subprocess.Popen", return_value=proc),
            patch("builtins.print") as mock_print,
        ):
            ok, msg = wait_db_logs_for_recovery_ready(
                "compose.yml",
                {},
                timeout_sec=5,
            )
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        proc.terminate.assert_called()
        mock_print.assert_called_once()
        args, kwargs = mock_print.call_args
        self.assertIn("database system is ready to accept connections", args[0])
        self.assertEqual(kwargs.get("end"), "")
        self.assertIs(kwargs.get("file"), sys.stderr)

    def test_success_silenced_skips_print(self) -> None:
        proc = MagicMock()
        proc.stdout = StringIO(
            "2026-01-01 00:00:00 UTC LOG:  database system is ready to accept connections\n"
        )
        proc.poll.return_value = None
        proc.wait.return_value = 0
        with (
            patch("catalpa_tooling.pgbackrest_db.subprocess.Popen", return_value=proc),
            patch("builtins.print") as mock_print,
        ):
            ok, msg = wait_db_logs_for_recovery_ready(
                "compose.yml",
                {"PGBR_RESTORE_SILENCE_DB_LOGS": "1"},
                timeout_sec=5,
            )
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        mock_print.assert_not_called()


if __name__ == "__main__":
    unittest.main()
