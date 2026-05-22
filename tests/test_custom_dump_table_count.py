"""Tests for ``pg_restore -l`` dump validation parsing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from catalpa_tooling.dev_cli import _custom_dump_table_count


def test_custom_dump_table_count_parses_pg_restore_listing(monkeypatch) -> None:
    listing = """\
;
; Archive created at 2026-05-21 06:20:51 +09
;     dbname: catalpa_db
;
219; 1259 29129 TABLE public auth_group catalpa
220; 1259 29134 SEQUENCE public auth_group_id_seq catalpa
"""
    proc = MagicMock(returncode=0, stdout=listing.encode())
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/pg_restore")
    monkeypatch.setattr("catalpa_tooling.dev_cli.run_cmd", lambda *_a, **_k: proc)

    assert _custom_dump_table_count(Path("/tmp/x.custom")) == 1
