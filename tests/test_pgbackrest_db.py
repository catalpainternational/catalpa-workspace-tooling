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

    def test_leading_double_dash_is_not_passed_to_pg_dump(self) -> None:
        script = _pg_dump_inner_script(["--", "--schema-only"])
        self.assertTrue(script.endswith("-Fc --schema-only"), script)


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
        self.assertNotIn("ALTER EXTENSION", block)
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
            calls[0][:11],
            # As postgres over the socket: a cluster restored from another host keeps that
            # host's postgres password, so a password over TCP cannot be relied on.
            ["docker", "compose", "-f", "compose.yml", "exec", "-T", "-u", "postgres", "db", "sh", "-c"],
        )
        script = calls[0][-1]
        self.assertIn("dropdb", script)
        self.assertIn("--force", script)
        self.assertIn("createdb", script)
        self.assertIn("-O", script)
        self.assertIn('APP_USER="${DJANGO_APP_DB_USER:-${DJANGO_DB_USER', script)
        self.assertIn('"$APP_DB"', script)
        self.assertNotIn("postgis", script)
        self.assertNotIn("PGPASSWORD", script)
        self.assertNotIn("-h ", script)


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
            "COMPOSE_PROJECT_NAME": "ligainan_dev",
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
            patch("catalpa_tooling.pgbackrest_db.check_backup_matches_image", return_value=0),
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
            patch("catalpa_tooling.pgbackrest_db.check_backup_matches_image", return_value=0),
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
            patch("catalpa_tooling.pgbackrest_db.check_backup_matches_image", return_value=0),
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
            cfg.has_metabase_fetch.return_value = False
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

    def test_successful_restore_runs_metabase_post_hooks_when_configured(self) -> None:
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
            patch("catalpa_tooling.pgbackrest_db.check_backup_matches_image", return_value=0),
            patch("catalpa_tooling.pgbackrest_db.run_interruptible") as run_int,
            patch("catalpa_tooling.pgbackrest_db._compose_up_db", return_value=0),
            patch(
                "catalpa_tooling.pgbackrest_db.wait_db_logs_for_recovery_ready",
                return_value=(True, ""),
            ),
            patch(
                "catalpa_tooling.pgbackrest_db.run_post_db_restore_manage_commands",
                return_value=0,
            ),
            patch(
                "catalpa_tooling.pgbackrest_db.run_post_metabase_db_restore_manage_commands",
                return_value=0,
            ) as mb_hooks,
        ):
            ok = MagicMock()
            ok.returncode = 0
            run_int.return_value = ok
            cfg = MagicMock()
            cfg.has_metabase_fetch.return_value = True
            rc = run_restore_offline(
                {"PGBR_STANZA": "main"},
                compose_file="compose.yml",
                env_name="dev",
                skip_confirm=True,
                config=cfg,
            )
        self.assertEqual(rc, 0)
        mb_hooks.assert_called_once_with(
            cfg,
            compose_file="compose.yml",
            env_add={"PGBR_STANZA": "main"},
            env_name="dev",
        )




_READ_ENV = {
    "PGBR_S3_READ_BUCKET": "b",
    "PGBR_S3_READ_REGION": "r",
    "PGBR_S3_READ_KEY": "k",
    "PGBR_S3_READ_SECRET": "s",
    "PGBR_S3_READ_REPO_PATH": "/p",
    "PGBR_S3_READ_STANZA": "main",
}


class TestVersionAndReadModeCheck(unittest.TestCase):
    def test_version_passes_no_stanza(self) -> None:
        """`pgbackrest version` rejects --stanza (ERROR [031])."""
        from catalpa_tooling.pgbackrest_db import run_version

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            calls.append(list(cmd))
            return MagicMock(returncode=0)

        with patch("catalpa_tooling.pgbackrest_db.run_cmd", side_effect=fake_run):
            self.assertEqual(run_version("compose.yml", dict(_READ_ENV)), 0)
        self.assertEqual(calls[0][-1], "version")
        self.assertFalse([a for a in calls[0] if a.startswith("--stanza")])

    def test_check_is_skipped_in_read_mode(self) -> None:
        """`pgbackrest check` needs archive_mode on, which a reader never has."""
        from catalpa_tooling.pgbackrest_db import run_check_online

        with (
            patch("catalpa_tooling.pgbackrest_db.run_cmd") as run,
            patch("sys.stderr", new_callable=StringIO) as err,
        ):
            self.assertEqual(run_check_online("compose.yml", dict(_READ_ENV)), 0)
        run.assert_not_called()
        self.assertIn("READ mode", err.getvalue())

    def test_version_runs_without_repository_settings(self) -> None:
        from catalpa_tooling.pgbackrest_db import run_version

        with patch(
            "catalpa_tooling.pgbackrest_db.run_cmd", return_value=MagicMock(returncode=0)
        ) as run:
            self.assertEqual(run_version("compose.yml", {}), 0)
        self.assertEqual(run.call_args.args[0][-1], "version")

    def test_check_runs_in_write_mode(self) -> None:
        from catalpa_tooling.pgbackrest_db import run_check_online

        write_env = {k.replace("_READ_", "_WRITE_"): v for k, v in _READ_ENV.items()}
        with patch(
            "catalpa_tooling.pgbackrest_db.run_cmd", return_value=MagicMock(returncode=0)
        ) as run:
            self.assertEqual(run_check_online("compose.yml", write_env), 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], "check")
        self.assertIn("--stanza=main", argv)


if __name__ == "__main__":
    unittest.main()


# The stanza history partisipa's production repo had after its 14 -> 18 upgrade (2026-10-25):
# older backups belong to cluster 1 (14), the newest to cluster 2 (18).
_INFO_AFTER_UPGRADE = """[{"name": "main",
  "db": [{"id": 1, "version": "14", "system-id": 1}, {"id": 2, "version": "18", "system-id": 2}],
  "backup": [{"label": "20261025-010003D", "database": {"id": 1}},
             {"label": "20261025-184608F", "database": {"id": 2}}]}]"""


class TestBackupMatchesImage(unittest.TestCase):
    def test_newest_backup_reads_its_own_cluster_version(self) -> None:
        from catalpa_tooling.pgbackrest_db import pg_major_of_newest_backup

        self.assertEqual(pg_major_of_newest_backup(_INFO_AFTER_UPGRADE), "18")

    def test_image_version_from_postgres_version(self) -> None:
        from catalpa_tooling.pgbackrest_db import pg_major_from_version_text

        text = "postgres (PostgreSQL) 14.24 (Debian 14.24-1.pgdg12+2)\n"
        self.assertEqual(pg_major_from_version_text(text), "14")

    def _check(self, stdout: str, extra=()) -> tuple[int, str]:
        from catalpa_tooling import pgbackrest_db

        done = MagicMock(returncode=0, stdout=stdout)
        err = StringIO()
        with (
            patch.object(pgbackrest_db, "resolve_stanza", return_value="main"),
            patch.object(pgbackrest_db.subprocess, "run", return_value=done) as run,
            patch.object(sys, "stderr", err),
        ):
            rc = pgbackrest_db.check_backup_matches_image(
                {}, compose_file="compose.yml", extra_pgbackrest_args=extra
            )
        self.run_calls = run.call_count
        return rc, err.getvalue()

    def test_refuses_a_backup_from_another_major(self) -> None:
        rc, err = self._check("postgres (PostgreSQL) 14.24\n---\n" + _INFO_AFTER_UPGRADE)
        self.assertEqual(rc, 1)
        self.assertIn("newest backup is PostgreSQL 18", err)
        self.assertIn("runs PostgreSQL 14", err)

    def test_goes_ahead_when_they_match(self) -> None:
        rc, _ = self._check("postgres (PostgreSQL) 18.6\n---\n" + _INFO_AFTER_UPGRADE)
        self.assertEqual(rc, 0)

    def test_goes_ahead_when_set_picks_another_backup(self) -> None:
        rc, _ = self._check("", extra=["--set=20261025-010003D"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.run_calls, 0)


class _FakeLogFollower:
    """`docker compose logs -f db` that prints ``lines`` and then keeps following."""

    def __init__(self, lines: list[str]) -> None:
        self.stdout = StringIO("".join(lines))
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self) -> None:
        self.terminated = True


_ENTRYPOINT_START = (
    "db-1  | PostgreSQL Database directory appears to contain a database; Skipping initialization\n"
)


class TestRecoveryWaitStopsOnFailure(unittest.TestCase):
    def _wait(self, lines: list[str]) -> tuple[bool, str]:
        from catalpa_tooling import pgbackrest_db

        fake = _FakeLogFollower(lines)
        with (
            patch.object(pgbackrest_db.subprocess, "Popen", return_value=fake),
            patch.object(sys, "stderr", StringIO()),
        ):
            return pgbackrest_db.wait_db_logs_for_recovery_ready(
                "compose.yml", {"PGBR_RESTORE_SILENCE_DB_LOGS": "1"}, timeout_sec=5
            )

    def test_stops_on_a_data_directory_from_another_major(self) -> None:
        ok, reason = self._wait(
            [
                _ENTRYPOINT_START,
                "db-1  | FATAL:  database files are incompatible with server\n",
            ]
        )
        self.assertFalse(ok)
        self.assertIn("database files are incompatible with server", reason)

    def test_stops_when_db_keeps_restarting(self) -> None:
        ok, reason = self._wait(
            [_ENTRYPOINT_START, "db-1  | FATAL:  could not open file\n", _ENTRYPOINT_START]
        )
        self.assertFalse(ok)
        self.assertIn("keeps restarting", reason)
        self.assertIn("could not open file", reason)

    def test_a_fatal_during_recovery_does_not_stop_the_wait(self) -> None:
        """A health check connecting too early gets a FATAL; recovery carries on."""
        ok, reason = self._wait(
            [
                _ENTRYPOINT_START,
                "db-1  | FATAL:  the database system is not yet accepting connections\n",
                "db-1  | LOG:  database system is ready to accept connections\n",
            ]
        )
        self.assertTrue(ok, reason)


class TestRestoreRefusesAnotherMajorFirst(unittest.TestCase):
    def test_a_mismatch_stops_before_the_confirmation_and_the_restore(self) -> None:
        """Refused before anything is stopped or asked: the user is not asked to confirm a
        restore that will not happen."""
        with (
            patch("catalpa_tooling.pgbackrest_db.validate_pgbackrest_env", return_value=None),
            patch("catalpa_tooling.pgbackrest_db.resolve_stanza", return_value="main"),
            patch("catalpa_tooling.pgbackrest_db.ensure_postgres_data_volume", return_value=0),
            patch(
                "catalpa_tooling.pgbackrest_db.ensure_pgbackrest_conf_before_restore",
                return_value=0,
            ),
            patch("catalpa_tooling.pgbackrest_db.check_backup_matches_image", return_value=1),
            patch("catalpa_tooling.pgbackrest_db.confirm_by_typing_env_name") as confirm,
            patch("catalpa_tooling.pgbackrest_db.db_service_responds", return_value=True),
            patch("catalpa_tooling.pgbackrest_db._compose_stop_db") as stop,
            patch("catalpa_tooling.pgbackrest_db.run_interruptible") as run_int,
            patch.object(sys, "stderr", StringIO()),
        ):
            rc = run_restore_offline(
                {"PGBR_STANZA": "main"},
                compose_file="compose.yml",
                env_name="dev",
                skip_confirm=False,
                config=MagicMock(),
            )
        self.assertEqual(rc, 1)
        confirm.assert_not_called()
        stop.assert_not_called()
        run_int.assert_not_called()


class TestBackupCheckEdges(unittest.TestCase):
    def _check(self, stdout: str, extra=()) -> tuple[int, str, int]:
        from catalpa_tooling import pgbackrest_db

        err = StringIO()
        with (
            patch.object(pgbackrest_db, "resolve_stanza", return_value="main"),
            patch.object(
                pgbackrest_db.subprocess, "run", return_value=MagicMock(returncode=0, stdout=stdout)
            ) as run,
            patch.object(sys, "stderr", err),
        ):
            rc = pgbackrest_db.check_backup_matches_image(
                {}, compose_file="compose.yml", extra_pgbackrest_args=extra
            )
        return rc, err.getvalue(), run.call_count

    def test_unreadable_versions_go_ahead_and_say_so(self) -> None:
        rc, err, _ = self._check("Error: no such service: db\n")
        self.assertEqual(rc, 0)
        self.assertIn("could not compare", err)

    def test_a_point_in_time_restore_is_not_checked(self) -> None:
        """pgBackRest picks the backup by the target time, which may predate an upgrade."""
        rc, _, runs = self._check("", extra=["--type=time", "--target=2026-10-24 12:00:00+00"])
        self.assertEqual(rc, 0)
        self.assertEqual(runs, 0)

    def test_set_given_as_its_own_word_is_not_checked_either(self) -> None:
        """The parser passes `--set X` as two words as often as `--set=X`."""
        rc, _, runs = self._check("", extra=["--set", "20261025-010003D"])
        self.assertEqual(rc, 0)
        self.assertEqual(runs, 0)
