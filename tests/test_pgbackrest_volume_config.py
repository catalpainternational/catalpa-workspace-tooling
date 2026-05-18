"""Tests for pgbackrest_volume_config (run: uv run python -m unittest discover -s tests -v)."""

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from catalpa_tooling.pgbackrest_volume_config import (
    caddy_data_volume_name,
    conflict_error_message,
    django_media_volume_name,
    ensure_postgres_data_volume,
    external_stack_volume_names,
    minimal_pgbackrest_baseline,
    postgres_data_volume_name,
    postgres_image_from_env,
    render_pgbackrest_ini,
    render_postgres_archive_conf,
    resolve_mode,
    stanza_create_allowed,
    volume_names,
)


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
        self.assertEqual(
            volume_names({}),
            ("pas_indmo_postgres_conf", "pas_indmo_pgbackrest_conf"),
        )
        self.assertEqual(
            volume_names({"COMPOSE_PROJECT_NAME": "myproj"}),
            ("myproj_postgres_conf", "myproj_pgbackrest_conf"),
        )

    def test_postgres_data_volume_name(self) -> None:
        self.assertEqual(postgres_data_volume_name({}), "pas_indmo_postgres_data")
        self.assertEqual(
            postgres_data_volume_name({"COMPOSE_PROJECT_NAME": "pas_indmo_local_deploy"}),
            "pas_indmo_local_deploy_postgres_data",
        )

    def test_django_media_and_caddy_volume_names(self) -> None:
        self.assertEqual(django_media_volume_name({}), "pas_indmo_django_media")
        self.assertEqual(
            django_media_volume_name({"COMPOSE_PROJECT_NAME": "myproj"}),
            "myproj_django_media",
        )
        self.assertEqual(caddy_data_volume_name({}), "pas_indmo_caddy_data")
        self.assertEqual(
            caddy_data_volume_name({"COMPOSE_PROJECT_NAME": "myproj"}),
            "myproj_caddy_data",
        )

    def test_external_stack_volume_names(self) -> None:
        self.assertEqual(
            external_stack_volume_names({}),
            (
                "pas_indmo_postgres_data",
                "pas_indmo_django_media",
                "pas_indmo_caddy_data",
                "pas_indmo_postgres_conf",
                "pas_indmo_pgbackrest_conf",
            ),
        )

    def test_postgres_image_from_env(self) -> None:
        self.assertEqual(
            postgres_image_from_env({}),
            "ghcr.io/catalpainternational/pas_indmo/indmo-postgres:latest",
        )
        self.assertEqual(
            postgres_image_from_env(
                {"STACK_IMAGE_REGISTRY": "reg.example/x", "STACK_IMAGE_TAG": "v1"}
            ),
            "reg.example/x/indmo-postgres:v1",
        )
        self.assertEqual(
            postgres_image_from_env({"Postgres_IMAGE": "custom:local"}),
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
        self.assertIn("pg1-path=/var/lib/postgresql/data", text)

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
        self.assertEqual(ensure_postgres_data_volume({}), 0)
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args[0][0][:3], ["docker", "volume", "inspect"])

    @patch("catalpa_tooling.pgbackrest_volume_config.run_cmd")
    def test_ensure_postgres_data_volume_creates_when_missing(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=1),
            MagicMock(returncode=0),
        ]
        self.assertEqual(ensure_postgres_data_volume({}), 0)
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(mock_run.call_args_list[1][0][0][:3], ["docker", "volume", "create"])

    @patch("catalpa_tooling.pgbackrest_volume_config.run_cmd")
    def test_ensure_postgres_data_volume_returns_1_on_create_failure(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=1),
            subprocess.CalledProcessError(1, ["docker", "volume", "create"]),
        ]
        self.assertEqual(ensure_postgres_data_volume({}), 1)


if __name__ == "__main__":
    unittest.main()
