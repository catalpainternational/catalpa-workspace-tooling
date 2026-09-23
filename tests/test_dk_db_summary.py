"""Regression: ``dk <env> db …`` deploy summary, and the stale-stack check before restore."""

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
    monkeypatch.setattr(
        env_handlers, "ensure_stack_matches_checkout", lambda *_a, **_k: (0, None)
    )

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


def _restore_ns(env_name: str) -> argparse.Namespace:
    return argparse.Namespace(
        env_name=env_name,
        env_command="db",
        db_command="restore",
        pgbackrest_restore_args=[],
        yes=True,
        tag=None,
    )


def _patch_env_handlers_for_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> "tuple[object, str]":
    """Minimal ``dk dev db restore`` wiring; returns (config, env_name)."""
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

    monkeypatch.setattr(env_handlers, "resolve_compose_file_from_info", lambda *_: "compose.yml")
    monkeypatch.setattr(
        env_handlers,
        "load_managed_deploy_context",
        lambda *_a, **_k: SimpleNamespace(
            env_add={},
            docker_host="",
            site_origin="http://example.test:9004",
            use_prepulled_registry=False,
            storage_volumes={},
            info=info,
        ),
    )
    monkeypatch.setattr(
        env_handlers,
        "resolve_env_with_compose_project",
        lambda _compose_file, env_add, **_kwargs: env_add,
    )
    return config, env_name


def test_db_restore_checks_the_stack_before_restoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_tooling: None
) -> None:
    """`db restore` must go through the stale-stack check before using the db image.

    Without it, an env with no pinned ``image_tag`` resolves ``STACK_IMAGE_TAG`` to the branch
    name; if that tag was never built the restore dies on ``manifest unknown`` partway through.
    The check is central, so this covers the whole `db` family rather than `restore` alone.
    """
    config, env_name = _patch_env_handlers_for_restore(tmp_path, monkeypatch)

    order: list[str] = []
    monkeypatch.setattr(
        env_handlers,
        "ensure_stack_matches_checkout",
        lambda *_a, **_k: (order.append("ensure") or 0, None),
    )
    monkeypatch.setattr(
        env_handlers,
        "run_unified_db_restore",
        lambda *_a, **_k: order.append("restore") or 0,
    )

    assert env_handlers.handle_env_command(_restore_ns(env_name), config) == 0
    assert order == ["ensure", "restore"]


def test_db_restore_aborts_when_stack_rebuild_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_tooling: None
) -> None:
    """A failed rebuild must stop the restore rather than let it run against a missing image."""
    config, env_name = _patch_env_handlers_for_restore(tmp_path, monkeypatch)

    monkeypatch.setattr(
        env_handlers, "ensure_stack_matches_checkout", lambda *_a, **_k: (7, None)
    )

    def _unreachable(*_a, **_k):
        raise AssertionError("run_unified_db_restore ran after the stack rebuild failed")

    monkeypatch.setattr(env_handlers, "run_unified_db_restore", _unreachable)

    assert env_handlers.handle_env_command(_restore_ns(env_name), config) == 7
