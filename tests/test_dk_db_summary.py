"""Regression: ``dk <env> db …`` must not print the deploy summary twice."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from catalpa_tooling import env_handlers
from catalpa_tooling.config import load_project_config
from tests.helpers import write_minimal_tooling_tree


def test_db_restore_prints_deploy_summary_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_tooling: None
) -> None:
    env_name = "dev"
    deploy_dir = tmp_path / "docker" / "envs" / env_name
    deploy_dir.mkdir(parents=True)
    info = {
        "name": env_name,
        "docker_host": "",
        "site_origin": "http://example.test:9004",
        "env": {},
    }
    (deploy_dir / "info.yaml").write_text(yaml.safe_dump(info), encoding="utf-8")

    write_minimal_tooling_tree(tmp_path)
    config = load_project_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    summary_calls = 0

    def fake_load_context(*_args, **_kwargs):
        nonlocal summary_calls
        summary_calls += 1
        return SimpleNamespace(
            env_add={},
            docker_host="",
            site_origin="http://example.test:9004",
            use_prepulled_registry=False,
            storage_volumes={},
            info=info,
        )

    monkeypatch.setattr(env_handlers, "resolve_compose_file_from_info", lambda *_: "compose.yml")
    monkeypatch.setattr(env_handlers, "load_managed_deploy_context", fake_load_context)
    monkeypatch.setattr(
        env_handlers,
        "resolve_env_with_compose_project",
        lambda _compose_file, env_add, **_kwargs: env_add,
    )
    monkeypatch.setattr(env_handlers, "run_unified_db_restore", lambda *_a, **_k: 0)

    ns = argparse.Namespace(
        env_name=env_name,
        env_command="db",
        db_command="restore",
        pgbackrest_restore_args=[],
        yes=True,
        tag=None,
    )
    rc = env_handlers.handle_env_command(ns, config)
    assert rc == 0
    assert summary_calls == 1
