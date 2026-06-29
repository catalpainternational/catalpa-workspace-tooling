"""Tests for pgbackrest_db helpers."""

import sys
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

from catalpa_tooling.pgbackrest_db import (
    _drop_create_app_database_psql_block,
    _log_level_argv_shell,
    _pg_dump_inner_script,
    _remove_interrupted_compose_run_db,
    _restore_db_logs_silenced,
    _restore_recovery_timeout_sec,
    build_restore_offline_argv,
    plan_restore_offline,
    run_backup,
    run_drop_create_app_database,
    run_restore_offline,
    wait_db_logs_for_recovery_ready,
)


class TestPgDumpInnerScript(unittest.TestCase):
    def test_uses_django_db_when_app_db_unset(self) -> None:
        script = _pg_dump_inner_script(None)
        self.assertIn('APP_DB="${DJANGO_APP_DB:-${DJANGO_DB', script)
        self.assertIn('-d "$APP_DB"', script)
        self.assertNotIn('"$DJANGO_APP_DB"', script)


class TestComposeExecPgbackrest(unittest.TestCase):
    def test_backup_runs_as_postgres_user(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            return m

        with patch("catalpa_tooling.pgbackrest_db.run_cmd", side_effect=fake_run):
            rc = run_backup("compose.yml", {"PGBR_STANZA": "main"}, "full")
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0][:10],
            [
                "docker",
                "compose",
                "-f",
                "compose.yml",
                "exec",
                "-T",
                "-u",
                "postgres",
                "db",
                "pgbackrest",
            ],
        )
        self.assertIn("--stanza=main", calls[0])
        self.assertIn("backup", calls[0])
        self.assertIn("--type=full", calls[0])


class TestDropCreateAppDatabase(unittest.TestCase):
    def test_psql_block_omits_postgis_by_default(self) -> None:
        block = _drop_create_app_database_psql_block(postgis=False)
        self.assertNotIn("postgis", block)

    def test_psql_block_preps_postgis_for_restore_when_enabled(self) -> None:
        block = _drop_create_app_database_psql_block(postgis=True)
        self.assertIn("CREATE EXTENSION IF NOT EXISTS postgis;", block)
        self.assertIn("GRANT ALL ON ALL TABLES IN SCHEMA public TO ${APP_USER};", block)

    def test_psql_block_includes_grants(self) -> None:
        block = _drop_create_app_database_psql_block(postgis=False)
        self.assertIn("GRANT ALL PRIVILEGES ON DATABASE", block)
        self.assertIn("GRANT ALL ON SCHEMA public", block)

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
        self.assertIn('APP_USER="${DJANGO_APP_DB_USER:-${DJANGO_DB_USER', script)
        self.assertIn('"$APP_DB"', script)
        self.assertNotIn("postgis", script)


class TestLogLevelArgvShell(unittest.TestCase):
    def test_restore_default_from_restore_key(self) -> None:
        s = _log_level_argv_shell({"PGBR_RESTORE_LOG_LEVEL_CONSOLE": "info"}, for_restore=True)
        self.assertIn("--log-level-console=info", s)

    def test_console_overrides_restore_key(self) -> None:
        s = _log_level_argv_shell(
            {
                "PGBR_LOG_LEVEL_CONSOLE": "warn",
                "PGBR_RESTORE_LOG_LEVEL_CONSOLE": "info",
            },
            for_restore=True,
        )
        self.assertIn("--log-level-console=warn", s)

    def test_empty_without_restore_keys(self) -> None:
        self.assertEqual(_log_level_argv_shell({}), "")
        self.assertEqual(_log_level_argv_shell({}, for_restore=True), "")


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


class TestRemoveInterruptedComposeRunDb(unittest.TestCase):
    def test_stops_and_removes_listed_oneoff_containers(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            m = MagicMock()
            if cmd[:2] == ["docker", "ps"]:
                m.stdout = "abc123\ndef456\n"
            m.returncode = 0
            return m

        with patch("catalpa_tooling.pgbackrest_db.run_cmd", side_effect=fake_run):
            _remove_interrupted_compose_run_db(
                "compose.yml",
                {"COMPOSE_PROJECT_NAME": "ligainan_dev"},
            )
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][:2], ["docker", "ps"])
        self.assertIn("label=com.docker.compose.oneoff=True", calls[0])
        self.assertIn("label=com.docker.compose.project=ligainan_dev", calls[0])
        self.assertEqual(calls[1], ["docker", "kill", "abc123", "def456"])
        self.assertEqual(calls[2], ["docker", "rm", "-f", "abc123", "def456"])


class TestPlanRestoreOffline(unittest.TestCase):
    def test_prints_repo_path_and_command(self) -> None:
        env = {
            "PGBR_S3_READ_BUCKET": "backups",
            "PGBR_S3_READ_REGION": "sgp1",
            "PGBR_S3_READ_KEY": "key",
            "PGBR_S3_READ_SECRET": "secret",
            "PGBR_S3_READ_REPO_PATH": "/app/prod/pgbackrest",
            "PGBR_S3_READ_STANZA": "main",
        }
        with (
            patch(
                "catalpa_tooling.pgbackrest_db.describe_pgbackrest_conf_status",
                return_value="volume config matches credentials",
            ),
            patch(
                "catalpa_tooling.pgbackrest_db.run_post_db_restore_manage_commands",
                return_value=0,
            ),
            patch("catalpa_tooling.pgbackrest_db.run_cmd") as mock_run,
        ):
            rc = plan_restore_offline(
                env,
                compose_file="compose.dev.yaml",
                env_name="dev",
                config=None,
            )
        self.assertEqual(rc, 0)
        mock_run.assert_not_called()
        argv = build_restore_offline_argv(env, compose_file="compose.dev.yaml")
        assert argv is not None
        self.assertIn("restore --delta", argv[-1])


class TestRunRestoreOfflineInterrupt(unittest.TestCase):
    def test_cancelled_restore_does_not_start_db(self) -> None:
        interrupted = MagicMock()
        interrupted.returncode = 130
        with (
            patch(
                "catalpa_tooling.pgbackrest_db.validate_pgbackrest_env",
                return_value=None,
            ),
            patch(
                "catalpa_tooling.pgbackrest_db.resolve_stanza",
                return_value="main",
            ),
            patch("catalpa_tooling.pgbackrest_db.ensure_postgres_data_volume", return_value=0),
            patch(
                "catalpa_tooling.pgbackrest_db.ensure_pgbackrest_conf_before_restore",
                return_value=0,
            ),
            patch("catalpa_tooling.pgbackrest_db.db_service_responds", return_value=False),
            patch(
                "catalpa_tooling.pgbackrest_db.run_interruptible",
                return_value=interrupted,
            ) as run_int,
            patch("catalpa_tooling.pgbackrest_db._compose_up_db") as up_db,
        ):
            rc = run_restore_offline(
                {"PGBR_STANZA": "main"},
                compose_file="compose.yml",
                env_name="dev",
                skip_confirm=True,
            )
        self.assertEqual(rc, 130)
        run_int.assert_called_once()
        up_db.assert_not_called()

    def test_cancelled_during_recovery_returns_130_without_hooks(self) -> None:
        ok = MagicMock()
        ok.returncode = 0
        with (
            patch(
                "catalpa_tooling.pgbackrest_db.validate_pgbackrest_env",
                return_value=None,
            ),
            patch("catalpa_tooling.pgbackrest_db.resolve_stanza", return_value="main"),
            patch("catalpa_tooling.pgbackrest_db.ensure_postgres_data_volume", return_value=0),
            patch(
                "catalpa_tooling.pgbackrest_db.ensure_pgbackrest_conf_before_restore",
                return_value=0,
            ),
            patch("catalpa_tooling.pgbackrest_db.db_service_responds", return_value=False),
            patch("catalpa_tooling.pgbackrest_db.run_interruptible", return_value=ok),
            patch("catalpa_tooling.pgbackrest_db._compose_up_db", return_value=0),
            patch(
                "catalpa_tooling.pgbackrest_db.wait_db_logs_for_recovery_ready",
                side_effect=KeyboardInterrupt,
            ),
            patch(
                "catalpa_tooling.pgbackrest_db.run_post_db_restore_manage_commands",
            ) as hooks,
        ):
            rc = run_restore_offline(
                {"PGBR_STANZA": "main"},
                compose_file="compose.yml",
                env_name="dev",
                skip_confirm=True,
                config=MagicMock(),
            )
        self.assertEqual(rc, 130)
        hooks.assert_not_called()


class TestRunRestoreOfflinePostHooks(unittest.TestCase):
    def test_successful_restore_runs_post_db_restore_hooks(self) -> None:
        with (
            patch(
                "catalpa_tooling.pgbackrest_db.validate_pgbackrest_env",
                return_value=None,
            ),
            patch("catalpa_tooling.pgbackrest_db.resolve_stanza", return_value="main"),
            patch("catalpa_tooling.pgbackrest_db.ensure_postgres_data_volume", return_value=0),
            patch(
                "catalpa_tooling.pgbackrest_db.ensure_pgbackrest_conf_before_restore",
                return_value=0,
            ),
            patch("catalpa_tooling.pgbackrest_db.db_service_responds", return_value=False),
            patch("catalpa_tooling.pgbackrest_db.run_interruptible") as run_int,
            patch("catalpa_tooling.pgbackrest_db._compose_up_db", return_value=0),
            patch(
                "catalpa_tooling.pgbackrest_db.wait_db_logs_for_recovery_ready",
                return_value=(True, ""),
            ),
            patch(
                "catalpa_tooling.pgbackrest_db.run_post_db_restore_manage_commands",
                return_value=0,
            ) as hooks,
        ):
            ok = MagicMock()
            ok.returncode = 0
            run_int.return_value = ok
            cfg = MagicMock()
            rc = run_restore_offline(
                {"PGBR_STANZA": "main"},
                compose_file="compose.yml",
                env_name="staging",
                skip_confirm=True,
                config=cfg,
            )
        self.assertEqual(rc, 0)
        hooks.assert_called_once_with(
            cfg,
            compose_file="compose.yml",
            env_add={"PGBR_STANZA": "main"},
            env_name="staging",
        )


if __name__ == "__main__":
    unittest.main()
