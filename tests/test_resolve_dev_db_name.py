"""Tests for ``resolve_dev_db_name``."""

from __future__ import annotations

from pathlib import Path

from catalpa_tooling.config import load_project_config, resolve_dev_db_name
from tests.test_fetch_media_config import _write_minimal_tooling


def test_resolve_db_name_from_fetch_dump_stem(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    (tmp_path / "tooling.yaml").write_text(
        (tmp_path / "tooling.yaml").read_text(encoding="utf-8").replace(
            "fetch_db_dump: dump", "fetch_db_dump: dumps/catalpa_db.custom"
        ),
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    assert resolve_dev_db_name(cfg) == "catalpa_db"


def test_resolve_db_name_fallback_overrides_stem(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    (tmp_path / "tooling.yaml").write_text(
        (tmp_path / "tooling.yaml").read_text(encoding="utf-8")
        + """
dev:
  reset_db:
    db_name_fallback: mydb
""",
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    assert resolve_dev_db_name(cfg) == "mydb"


def test_resolve_db_name_project_when_no_dump_stem(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    (tmp_path / "tooling.yaml").write_text(
        (tmp_path / "tooling.yaml").read_text(encoding="utf-8").replace(
            "fetch_db_dump: dump", "fetch_db_dump: backups/plain"
        ),
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    assert resolve_dev_db_name(cfg) == "test_db"
