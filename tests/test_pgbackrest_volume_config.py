"""Tests for pgbackrest_volume_config (run: uv run python -m unittest discover -s tests -v)."""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalpa_tooling.config import load_project_config
from catalpa_tooling.pgbackrest_volume_config import (
    PgbackrestRepoSettings,
    caddy_data_volume_name,
    conflict_error_message,
    db_compose_volume_names,
    django_media_volume_name,
    ensure_db_compose_volumes,
    ensure_pgbackrest_conf_before_restore,
    ensure_postgres_data_volume,
    expected_pgbackrest_repo_settings,
    external_stack_volume_names,
    minimal_pgbackrest_baseline,
    pgbackrest_managed_conf_materialized,
    pgdata_volume_mount,
    postgres_data_volume_name,
    postgres_image_from_env,
    render_pgbackrest_ini,
    render_postgres_archive_conf,
    repo_settings_match,
    resolve_mode,
    stanza_create_allowed,
    volume_names,
    _parse_pgbackrest_managed_ini,
)

_MINIMAL_CONFIG = load_project_config(
    Path(__file__).resolve().parent / "fixtures" / "minimal_project"
)


def _sample_read_env() -> dict[str, str]:
    return {
        "COMPOSE_PROJECT_NAME": "app_compose",
        "PGBR_S3_READ_BUCKET": "backups",
        "PGBR_S3_READ_REGION": "sgp1",
        "PGBR_S3_READ_KEY": "key",
        "PGBR_S3_READ_SECRET": "secret",
        "PGBR_S3_READ_REPO_PATH": "/app/prod/pgbackrest",
        "PGBR_S3_READ_STANZA": "main",
    }


class TestPgbackrestVolumeConfig(unittest.TestCase):
    def test_resolve_mode_none(self) -> None:
        self.assertEqual(resolve_mode({}), "none")
        self.assertEqual(resolve_mode({"PGBR_S3_WRITE_BUCKET": ""}), "none")

    def test_resolve_mode_write(self) -> None:
        self.assertEqual(
            resolve_mode({"PGBR_S3_WRITE_BUCKET": "b"}),
            "write",
        )

    def test_resolve_mode_read(self) -> None:
        self.assertEqual(
            resolve_mode({"PGBR_S3_READ_BUCKET": "b"}),
            "read",
        )

    def test_stanza_create_allowed_write_only(self) -> None:
        self.assertFalse(stanza_create_allowed({}))
        self.assertFalse(stanza_create_allowed({"PGBR_S3_READ_BUCKET": "b"}))
        self.assertFalse(
            stanza_create_allowed(
                {"PGBR_S3_WRITE_BUCKET": "w", "PGBR_S3_READ_BUCKET": "r"}
            )
        )
        self.assertTrue(stanza_create_allowed({"PGBR_S3_WRITE_BUCKET": "b"}))

    def test_conflict_error_message(self) -> None:
        self.assertIsNone(conflict_error_message({}))
        self.assertIsNone(
            conflict_error_message({"PGBR_S3_WRITE_BUCKET": "x"})
        )
        msg = conflict_error_message(
            {
                "PGBR_S3_WRITE_BUCKET": "w",
                "PGBR_S3_READ_BUCKET": "r",
            }
        )
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertIn("mutually exclusive", msg)

    def test_volume_names(self) -> None:
        cfg = _MINIMAL_CONFIG
        self.assertEqual(
            volume_names({}, config=cfg),
            ("app_compose_postgres_conf", "app_compose_pgbackrest_conf"),
        )
        self.assertEqual(
            volume_names({"COMPOSE_PROJECT_NAME": "myproj"}, config=cfg),
            ("myproj_postgres_conf", "myproj_pgbackrest_conf"),
        )

    def test_postgres_data_volume_name(self) -> None:
        cfg = _MINIMAL_CONFIG
        self.assertEqual(
            postgres_data_volume_name({}, config=cfg),
            "app_compose_postgres_data",
        )
        self.assertEqual(
            postgres_data_volume_name(
                {"COMPOSE_PROJECT_NAME": "app_compose_local_deploy"}, config=cfg
            ),
            "app_compose_local_deploy_postgres_data",
        )

    def test_postgres_data_volume_name_custom_key(self) -> None:
        from catalpa_tooling.config import PgbackrestOpsConfig

        cfg = MagicMock()
        cfg.ops.pgbackrest = PgbackrestOpsConfig(
            postgres_conf="x.conf",
            pgbackrest_conf="y.conf",
            default_registry="ghcr.io/example",
            restore_temp_prefix="pfx_",
            data_volume="db_data",
            pg1_path="/var/lib/postgresql/18/docker",
        )
        self.assertEqual(
            postgres_data_volume_name({"COMPOSE_PROJECT_NAME": "myproj"}, config=cfg),
            "myproj_db_data",
        )

    def test_django_media_and_caddy_volume_names(self) -> None:
        cfg = _MINIMAL_CONFIG
        self.assertEqual(
            django_media_volume_name({}, config=cfg),
            "app_compose_django_media",
        )
        self.assertEqual(
            django_media_volume_name({"COMPOSE_PROJECT_NAME": "myproj"}, config=cfg),
            "myproj_django_media",
        )
        self.assertEqual(caddy_data_volume_name({}, config=cfg), "app_compose_caddy_data")
        self.assertEqual(
            caddy_data_volume_name({"COMPOSE_PROJECT_NAME": "myproj"}, config=cfg),
            "myproj_caddy_data",
        )

    def test_external_stack_volume_names(self) -> None:
        cfg = _MINIMAL_CONFIG
        self.assertEqual(
            external_stack_volume_names({}, config=cfg),
            (
                "app_compose_postgres_data",
                "app_compose_django_media",
                "app_compose_caddy_data",
                "app_compose_postgres_conf",
                "app_compose_pgbackrest_conf",
            ),
        )

    def test_postgres_image_from_env(self) -> None:
        cfg = _MINIMAL_CONFIG
        self.assertEqual(
            postgres_image_from_env({}, config=cfg),
            "ghcr.io/example/app/app-db:latest",
        )
        self.assertEqual(
            postgres_image_from_env(
                {"STACK_IMAGE_REGISTRY": "reg.example/x", "STACK_IMAGE_TAG": "v1"},
                config=cfg,
            ),
            "reg.example/x/app-db:v1",
        )
        self.assertEqual(
            postgres_image_from_env({"Postgres_IMAGE": "custom:local"}, config=cfg),
            "custom:local",
        )

    def test_render_pgbackrest_write(self) -> None:
        vm = {
            "BUCKET": "mybucket",
            "REGION": "eu-west-1",
            "KEY": "k",
            "SECRET": "s",
            "REPO_PATH": "/repo",
            "STANZA": "main",
            "ENDPOINT": "https://s3.example.com",
        }
        text = render_pgbackrest_ini("write", vm, {})
        self.assertIn("repo1-type=s3", text)
        self.assertIn("repo1-s3-bucket=mybucket", text)
        self.assertIn("repo1-s3-endpoint=https://s3.example.com", text)
        self.assertIn("repo1-bundle=y", text)
        self.assertIn("repo1-block=y", text)
        self.assertIn("process-max=2", text)
        self.assertIn("archive-async=y", text)
        self.assertIn("repo1-retention-full-type=time", text)
        self.assertIn("repo1-retention-full=30", text)
        self.assertIn("[global:archive-push]", text)
        self.assertIn("compress-level=3", text)
        self.assertIn("[main]", text)
        self.assertIn("pg1-path=/var/lib/postgresql/18/docker", text)

    def test_render_pgbackrest_write_retention_override(self) -> None:
        vm = {
            "BUCKET": "b",
            "REGION": "r",
            "KEY": "k",
            "SECRET": "s",
            "REPO_PATH": "/p",
            "STANZA": "main",
            "RETENTION_FULL": "14",
        }
        text = render_pgbackrest_ini("write", vm, {})
        self.assertIn("repo1-retention-full=14", text)

    def test_render_pgbackrest_tuning_env_override(self) -> None:
        vm = {
            "BUCKET": "b",
            "REGION": "r",
            "KEY": "k",
            "SECRET": "s",
            "REPO_PATH": "/p",
            "STANZA": "main",
        }
        text = render_pgbackrest_ini(
            "write",
            vm,
            {"PGBR_PROCESS_MAX": "8", "PGBR_REPO1_BUNDLE": "no"},
        )
        self.assertIn("process-max=8", text)
        self.assertIn("repo1-bundle=n", text)

    def test_render_pgbackrest_read_includes_tuning_defaults(self) -> None:
        vm = {
            "BUCKET": "b",
            "REGION": "r",
            "KEY": "k",
            "SECRET": "s",
            "REPO_PATH": "/p",
            "STANZA": "st",
        }
        text = render_pgbackrest_ini("read", vm, {})
        self.assertIn("repo1-retention-full=30", text)
        self.assertIn("compress-level=3", text)

    def test_render_postgres_archive(self) -> None:
        self.assertIn(
            "archive_command = 'pgbackrest --stanza=main archive-push %p'",
            render_postgres_archive_conf("main"),
        )

    def test_minimal_baseline(self) -> None:
        self.assertIn("[global]", minimal_pgbackrest_baseline())

    @patch("catalpa_tooling.pgbackrest_volume_config.run_cmd")
    def test_ensure_postgres_data_volume_skips_create_when_present(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        self.assertEqual(ensure_postgres_data_volume({}, config=_MINIMAL_CONFIG), 0)
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args[0][0][:3], ["docker", "volume", "inspect"])

    @patch("catalpa_tooling.pgbackrest_volume_config.run_cmd")
    def test_ensure_postgres_data_volume_creates_when_missing(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=1),
            MagicMock(returncode=0),
        ]
        env = {"COMPOSE_PROJECT_NAME": "jid-full"}
        self.assertEqual(ensure_postgres_data_volume(env, config=_MINIMAL_CONFIG), 0)
        self.assertEqual(mock_run.call_count, 2)
        create_cmd = mock_run.call_args_list[1][0][0]
        self.assertEqual(create_cmd[:3], ["docker", "volume", "create"])
        self.assertIn(
            "com.docker.compose.project=jid-full",
            create_cmd,
        )
        self.assertIn(
            "com.docker.compose.volume=postgres_data",
            create_cmd,
        )
        self.assertEqual(create_cmd[-1], "jid-full_postgres_data")

    @patch("catalpa_tooling.pgbackrest_volume_config.run_cmd")
    def test_ensure_postgres_data_volume_returns_1_on_create_failure(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=1),
            subprocess.CalledProcessError(1, ["docker", "volume", "create"]),
        ]
        self.assertEqual(ensure_postgres_data_volume({}, config=_MINIMAL_CONFIG), 1)

    def test_db_compose_volume_names(self) -> None:
        env = {"COMPOSE_PROJECT_NAME": "jid-full"}
        self.assertEqual(
            db_compose_volume_names(env),
            (
                "jid-full_postgres_data",
                "jid-full_postgres_conf",
                "jid-full_pgbackrest_conf",
            ),
        )

    @patch("catalpa_tooling.pgbackrest_volume_config.run_cmd")
    def test_ensure_db_compose_volumes_inspects_all_db_mounts(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        env = {"COMPOSE_PROJECT_NAME": "jid-full"}
        self.assertEqual(ensure_db_compose_volumes(env), 0)
        inspected = [c[0][0][3] for c in mock_run.call_args_list]
        self.assertEqual(
            inspected,
            [
                "jid-full_postgres_data",
                "jid-full_postgres_conf",
                "jid-full_pgbackrest_conf",
            ],
        )


class TestPgbackrestManagedConfMaterialized(unittest.TestCase):
    @patch("catalpa_tooling.pgbackrest_volume_config.run_cmd")
    def test_true_when_probe_succeeds(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        env = {"PGBR_S3_READ_STANZA": "main", "PGBR_S3_READ_BUCKET": "b"}
        self.assertTrue(pgbackrest_managed_conf_materialized(env, config=_MINIMAL_CONFIG))

    @patch("catalpa_tooling.pgbackrest_volume_config.run_cmd")
    def test_false_when_probe_fails(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1)
        env = {"PGBR_S3_READ_STANZA": "main", "PGBR_S3_READ_BUCKET": "b"}
        self.assertFalse(pgbackrest_managed_conf_materialized(env, config=_MINIMAL_CONFIG))

    def test_false_in_none_mode(self) -> None:
        self.assertFalse(pgbackrest_managed_conf_materialized({}, config=_MINIMAL_CONFIG))


class TestParsePgbackrestManagedIni(unittest.TestCase):
    def test_parses_global_and_stanza(self) -> None:
        ini = render_pgbackrest_ini(
            "read",
            {
                "BUCKET": "backups",
                "REGION": "sgp1",
                "KEY": "k",
                "SECRET": "s",
                "REPO_PATH": "/app/prod/pgbackrest",
                "STANZA": "main",
            },
            {},
            pg1_path="/var/lib/postgresql/18/docker",
        )
        parsed = _parse_pgbackrest_managed_ini(ini)
        assert parsed is not None
        self.assertEqual(parsed.stanza, "main")
        self.assertEqual(parsed.repo_path, "/app/prod/pgbackrest")
        self.assertEqual(parsed.bucket, "backups")
        self.assertEqual(parsed.pg1_path, "/var/lib/postgresql/18/docker")

    def test_expected_from_read_env(self) -> None:
        expected = expected_pgbackrest_repo_settings(_sample_read_env())
        assert expected is not None
        self.assertEqual(expected.repo_path, "/app/prod/pgbackrest")


class TestEnsurePgbackrestConfBeforeRestore(unittest.TestCase):
    @patch(
        "catalpa_tooling.pgbackrest_volume_config.read_managed_pgbackrest_repo_settings",
    )
    @patch(
        "catalpa_tooling.pgbackrest_volume_config.expected_pgbackrest_repo_settings",
    )
    @patch("catalpa_tooling.pgbackrest_volume_config.resolve_mode", return_value="read")
    def test_skips_when_volume_matches_credentials(
        self,
        _mode: MagicMock,
        mock_expected: MagicMock,
        mock_read: MagicMock,
    ) -> None:
        settings = expected_pgbackrest_repo_settings(_sample_read_env())
        assert settings is not None
        mock_expected.return_value = settings
        mock_read.return_value = settings
        self.assertEqual(
            ensure_pgbackrest_conf_before_restore(_sample_read_env(), config=_MINIMAL_CONFIG),
            0,
        )

    @patch(
        "catalpa_tooling.pgbackrest_volume_config.materialize_configs",
        return_value=0,
    )
    @patch(
        "catalpa_tooling.cli_confirm.confirm_yes_default_no",
        return_value=True,
    )
    @patch(
        "catalpa_tooling.pgbackrest_volume_config.read_managed_pgbackrest_repo_settings",
        return_value=None,
    )
    @patch("catalpa_tooling.pgbackrest_volume_config.resolve_mode", return_value="read")
    def test_runs_configure_after_yes(
        self,
        _mode: MagicMock,
        _mock_read: MagicMock,
        _confirm: MagicMock,
        mock_mat_cfg: MagicMock,
    ) -> None:
        env = _sample_read_env()
        self.assertEqual(ensure_pgbackrest_conf_before_restore(env, config=_MINIMAL_CONFIG), 0)
        mock_mat_cfg.assert_called_once()

    @patch(
        "catalpa_tooling.pgbackrest_volume_config.read_managed_pgbackrest_repo_settings",
        return_value=None,
    )
    @patch(
        "catalpa_tooling.cli_confirm.confirm_yes_default_no",
        return_value=False,
    )
    @patch("catalpa_tooling.pgbackrest_volume_config.resolve_mode", return_value="read")
    def test_cancelled_when_user_declines(
        self,
        _mode: MagicMock,
        _confirm: MagicMock,
        _mock_read: MagicMock,
    ) -> None:
        env = _sample_read_env()
        self.assertEqual(ensure_pgbackrest_conf_before_restore(env, config=_MINIMAL_CONFIG), 1)

    @patch(
        "catalpa_tooling.pgbackrest_volume_config.materialize_configs",
        return_value=0,
    )
    @patch(
        "catalpa_tooling.pgbackrest_volume_config.read_managed_pgbackrest_repo_settings",
    )
    @patch("catalpa_tooling.pgbackrest_volume_config.resolve_mode", return_value="read")
    def test_rematerializes_when_repo_path_stale(
        self,
        _mode: MagicMock,
        mock_read: MagicMock,
        mock_mat_cfg: MagicMock,
    ) -> None:
        expected = expected_pgbackrest_repo_settings(_sample_read_env())
        assert expected is not None
        stale = PgbackrestRepoSettings(
            stanza=expected.stanza,
            repo_path="/old/path",
            bucket=expected.bucket,
            region=expected.region,
            endpoint=expected.endpoint,
            pg1_path=expected.pg1_path,
        )
        mock_read.return_value = stale
        self.assertEqual(
            ensure_pgbackrest_conf_before_restore(
                _sample_read_env(), skip_configure_confirm=True, config=_MINIMAL_CONFIG
            ),
            0,
        )
        mock_mat_cfg.assert_called_once()

    @patch(
        "catalpa_tooling.pgbackrest_volume_config.read_managed_pgbackrest_repo_settings",
        return_value=None,
    )
    @patch(
        "catalpa_tooling.pgbackrest_volume_config.materialize_configs",
        return_value=0,
    )
    @patch("catalpa_tooling.pgbackrest_volume_config.resolve_mode", return_value="read")
    def test_auto_configure_with_skip_confirm(
        self,
        _mode: MagicMock,
        mock_mat_cfg: MagicMock,
        _mock_read: MagicMock,
    ) -> None:
        env = _sample_read_env()
        self.assertEqual(
            ensure_pgbackrest_conf_before_restore(
                env, skip_configure_confirm=True, config=_MINIMAL_CONFIG
            ),
            0,
        )
        mock_mat_cfg.assert_called_once()


class TestRepoSettingsMatch(unittest.TestCase):
    def test_match_requires_all_fields(self) -> None:
        a = expected_pgbackrest_repo_settings(_sample_read_env())
        b = expected_pgbackrest_repo_settings(_sample_read_env())
        assert a is not None and b is not None
        self.assertTrue(repo_settings_match(a, b))
        stale = PgbackrestRepoSettings(
            stanza=a.stanza,
            repo_path="/other",
            bucket=a.bucket,
            region=a.region,
            endpoint=a.endpoint,
            pg1_path=a.pg1_path,
        )
        self.assertFalse(repo_settings_match(stale, a))


class TestPgdataVolumeMount(unittest.TestCase):
    def test_pg18_mounts_parent_tree(self) -> None:
        self.assertEqual(
            pgdata_volume_mount("/var/lib/postgresql/18/docker"),
            "/var/lib/postgresql",
        )

    def test_legacy_data_mounts_directly(self) -> None:
        self.assertEqual(
            pgdata_volume_mount("/var/lib/postgresql/data"),
            "/var/lib/postgresql/data",
        )


if __name__ == "__main__":
    unittest.main()
