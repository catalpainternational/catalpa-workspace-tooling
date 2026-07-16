"""Tests for ``run_bkp_db_stanza_create_flow`` and ``run_bkp_db_init`` orchestration."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from catalpa_tooling.pgbackrest_db import run_bkp_db_init, run_bkp_db_stanza_create_flow

_FLOW_ENV = {"COMPOSE_PROJECT_NAME": "testproj"}


class TestRunBkpDbStanzaCreateFlow(unittest.TestCase):
    @patch("catalpa_tooling.pgbackrest_db.run_pgbackrest_stanza_create", return_value=0)
    @patch("catalpa_tooling.pgbackrest_db._pgdata_has_control_file", return_value=True)
    @patch(
        "catalpa_tooling.pgbackrest_db.pgbackrest_stanza_exists_in_repo",
        return_value=False,
    )
    @patch(
        "catalpa_tooling.pgbackrest_db.pgbackrest_managed_conf_materialized",
        return_value=True,
    )
    def test_runs_stanza_create_when_ready(
        self,
        _managed: object,
        _exists: object,
        _pgdata: object,
        stanza_create: object,
    ) -> None:
        self.assertEqual(
            run_bkp_db_stanza_create_flow(
                "compose.yml", _FLOW_ENV, image="img:tag", config=None
            ),
            0,
        )
        stanza_create.assert_called_once()

    @patch("catalpa_tooling.pgbackrest_db.run_pgbackrest_stanza_create")
    @patch("catalpa_tooling.pgbackrest_db.ensure_db_service_running", return_value=0)
    @patch("catalpa_tooling.pgbackrest_db._pgdata_has_control_file")
    @patch(
        "catalpa_tooling.pgbackrest_db.pgbackrest_stanza_exists_in_repo",
        return_value=False,
    )
    @patch(
        "catalpa_tooling.pgbackrest_db.pgbackrest_managed_conf_materialized",
        return_value=True,
    )
    def test_starts_db_when_pgdata_missing(
        self,
        _managed: object,
        _exists: object,
        pgdata: object,
        ensure_db: object,
        stanza_create: object,
    ) -> None:
        pgdata.side_effect = [False, True]
        stanza_create.return_value = 0
        self.assertEqual(
            run_bkp_db_stanza_create_flow(
                "compose.yml", _FLOW_ENV, image="img:tag", config=None
            ),
            0,
        )
        ensure_db.assert_called_once_with(
            "compose.yml", _FLOW_ENV, config=None, dk_env_name=None
        )
        stanza_create.assert_called_once()

    @patch("catalpa_tooling.pgbackrest_db.run_pgbackrest_stanza_create")
    @patch(
        "catalpa_tooling.pgbackrest_db.pgbackrest_stanza_exists_in_repo",
        return_value=True,
    )
    @patch(
        "catalpa_tooling.pgbackrest_db.pgbackrest_managed_conf_materialized",
        return_value=True,
    )
    def test_skips_when_stanza_exists(
        self,
        _managed: object,
        _exists: object,
        stanza_create: object,
    ) -> None:
        self.assertEqual(
            run_bkp_db_stanza_create_flow(
                "compose.yml", _FLOW_ENV, image="img:tag", config=None
            ),
            0,
        )
        stanza_create.assert_not_called()

    @patch("catalpa_tooling.pgbackrest_db.materialize_configs", return_value=0)
    @patch(
        "catalpa_tooling.pgbackrest_db.pgbackrest_managed_conf_materialized",
        return_value=False,
    )
    @patch("catalpa_tooling.pgbackrest_db.run_pgbackrest_stanza_create", return_value=0)
    @patch("catalpa_tooling.pgbackrest_db._pgdata_has_control_file", return_value=True)
    @patch(
        "catalpa_tooling.pgbackrest_db.pgbackrest_stanza_exists_in_repo",
        return_value=False,
    )
    def test_materializes_when_conf_missing(
        self,
        _exists: object,
        _pgdata: object,
        _stanza: object,
        managed: object,
        materialize: object,
    ) -> None:
        self.assertEqual(
            run_bkp_db_stanza_create_flow(
                "compose.yml", _FLOW_ENV, image="img:tag", config=None
            ),
            0,
        )
        materialize.assert_called_once()


class TestRunBkpDbInit(unittest.TestCase):
    @patch(
        "catalpa_tooling.pgbackrest_db.run_bkp_db_stanza_create_flow",
        return_value=0,
    )
    @patch(
        "catalpa_tooling.pgbackrest_db.ensure_db_service_running",
        return_value=0,
    )
    @patch("catalpa_tooling.pgbackrest_db.materialize_configs", return_value=0)
    @patch(
        "catalpa_tooling.pgbackrest_db.ensure_external_stack_volumes",
        return_value=0,
    )
    def test_init_orchestrates_all_steps(
        self,
        ensure_vols: object,
        materialize: object,
        ensure_db: object,
        stanza_flow: object,
    ) -> None:
        self.assertEqual(
            run_bkp_db_init("compose.yml", _FLOW_ENV, image="img:tag", config=None),
            0,
        )
        ensure_vols.assert_called_once()
        materialize.assert_called_once()
        ensure_db.assert_called_once()
        stanza_flow.assert_called_once_with(
            "compose.yml", _FLOW_ENV, image="img:tag", config=None, dk_env_name=None
        )


if __name__ == "__main__":
    unittest.main()
