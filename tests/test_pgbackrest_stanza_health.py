"""Tests for pgBackRest ``info`` stanza health parsing (idempotent stanza-create)."""

from __future__ import annotations

import unittest

from catalpa_tooling.pgbackrest_volume_config import parse_pgbackrest_info_stanza_healthy


class TestParsePgbackrestInfoStanzaHealthy(unittest.TestCase):
    def test_text_ok(self) -> None:
        out = "stanza: main\n    status: ok\n    db (current)\n"
        self.assertTrue(parse_pgbackrest_info_stanza_healthy(out, "", stanza="main"))

    def test_text_missing_stanza_path_not_ok(self) -> None:
        out = "stanza: main\n    status: error (missing stanza path)\n"
        self.assertFalse(parse_pgbackrest_info_stanza_healthy(out, "", stanza="main"))

    def test_text_exit_zero_style_error_still_false(self) -> None:
        """Regression: pgbackrest info can exit 0 while printing status: error."""
        out = "stanza: main\n    status: error (missing stanza path)\n"
        self.assertFalse(parse_pgbackrest_info_stanza_healthy(out, "", stanza="main"))

    def test_json_ok(self) -> None:
        out = '[{"name": "main", "status": {"code": 0, "message": "ok"}}]'
        self.assertTrue(parse_pgbackrest_info_stanza_healthy(out, "", stanza="main"))

    def test_json_error_code(self) -> None:
        out = '[{"name": "main", "status": {"code": 25, "message": "missing stanza path"}}]'
        self.assertFalse(parse_pgbackrest_info_stanza_healthy(out, "", stanza="main"))

    def test_wrong_stanza_name(self) -> None:
        out = "stanza: other\n    status: ok\n"
        self.assertFalse(parse_pgbackrest_info_stanza_healthy(out, "", stanza="main"))

    def test_empty_output(self) -> None:
        self.assertFalse(parse_pgbackrest_info_stanza_healthy("", "", stanza="main"))


if __name__ == "__main__":
    unittest.main()
