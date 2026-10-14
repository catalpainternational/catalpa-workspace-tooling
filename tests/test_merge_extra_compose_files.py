"""``merge_extra_compose_files`` is safe to call on its own output (issues #76, #83)."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dc_backup.hosts import merge_extra_compose_files


def test_merge_skips_none_groups() -> None:
    assert merge_extra_compose_files(None, ["x"]) == ["x"]
    assert merge_extra_compose_files(["x"], None) == ["x"]


def test_merge_returns_none_for_all_empty() -> None:
    assert merge_extra_compose_files(None, None) is None
    assert merge_extra_compose_files([], None) is None
    assert merge_extra_compose_files() is None


def test_merge_preserves_order_across_groups() -> None:
    assert merge_extra_compose_files(["a", "b"], None, ["c"]) == ["a", "b", "c"]


def test_build_reaches_compose_without_proxy_or_tls_files(
    minimal_project: ProjectConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dk dev build django`` on an env with no overrides used to die in the merge."""
    from catalpa_tooling import env_handlers

    seen: dict[str, object] = {}

    def fake_compose(compose_file: str, *args: str, **kwargs: object) -> object:
        seen["args"] = list(args)
        seen["extra"] = kwargs.get("extra_compose_files")
        return subprocess.CompletedProcess([compose_file, *args], 0)

    monkeypatch.setattr(env_handlers, "_compose", fake_compose)
    monkeypatch.setattr(env_handlers, "should_materialize_for_compose", lambda _a: False)
    monkeypatch.setattr(
        env_handlers, "local_proxy_extra_compose_files", lambda *a, **k: []
    )
    monkeypatch.setattr(
        env_handlers, "dc_backup_tls_extra_compose_files", lambda *a, **k: []
    )

    rc = env_handlers._run_compose_path(
        argparse.Namespace(dry_run=False, yes=True, tag=None),
        minimal_project,
        info={},
        compose_file="compose.yml",
        env_add={},
        env_name="dev",
        compose_args=["build", "django"],
        use_prepulled_registry=False,
        site_origin="",
        docker_host="",
        storage_volumes={},
    )

    assert rc == 0
    assert seen["args"] == ["build", "django"]
    # The label override is the only extra file, and it is cleaned up afterwards.
    extra = seen["extra"]
    assert isinstance(extra, list) and len(extra) == 1
    assert not Path(extra[0]).exists()
