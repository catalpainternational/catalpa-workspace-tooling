"""The db image name comes from the resolved compose file, not a guess from the registry.

Tooling used to assemble the name as ``{registry}/{project}-postgres:{tag}``. A local project
with no registry (``image_registry: ""``, as QUICKSTART recommends for dev) had nothing to
assemble it from, and on Podman an unqualified name is stored differently from the guess.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import yaml

from catalpa_tooling import env_handlers, stack_commands, stale_stack
from catalpa_tooling.config import ProjectConfig, ProjectConfigError, load_project_config
from catalpa_tooling.pgbackrest_volume_config import (
    postgres_image_from_env,
    set_db_image_from_compose,
)
from tests.helpers import write_minimal_tooling_tree

COMPOSE_DB_IMAGE = "localhost/minimal-postgres:my-branch"


@pytest.fixture
def config(tmp_path: Path, isolated_tooling: None) -> ProjectConfig:
    """A local-only project: ``ops: {}`` and no image registry."""
    write_minimal_tooling_tree(tmp_path)
    manifest = tmp_path / "tooling.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["ops"] = {}
    manifest.write_text(yaml.safe_dump(data), encoding="utf-8")
    (tmp_path / "docker" / "images.yaml").write_text('image_registry: ""\n', encoding="utf-8")
    return load_project_config(tmp_path)


def _fake_compose_config(monkeypatch: pytest.MonkeyPatch, config: ProjectConfig, *, rc: int = 0):
    class _Proc:
        returncode = rc
        stdout = json.dumps(
            {"services": {config.stack_service("db"): {"image": COMPOSE_DB_IMAGE, "build": {}}}}
        )
        stderr = ""

    monkeypatch.setattr(stale_stack, "run_cmd", lambda *_a, **_k: _Proc())


def test_without_a_registry_the_guess_fails(config: ProjectConfig) -> None:
    """The premise: this project gives tooling nothing to build the name from."""
    with pytest.raises(ProjectConfigError, match="default_registry"):
        postgres_image_from_env({"STACK_IMAGE_TAG": "my-branch"}, config=config)


def test_the_db_image_is_the_one_compose_resolves(
    config: ProjectConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_compose_config(monkeypatch, config)
    env = {"STACK_IMAGE_TAG": "my-branch"}
    set_db_image_from_compose(config, "compose.yml", env)
    assert postgres_image_from_env(env, config=config) == COMPOSE_DB_IMAGE


def test_an_explicit_db_image_is_kept(
    config: ProjectConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pinned envs set ``Postgres_IMAGE`` themselves; compose must not override it."""
    _fake_compose_config(monkeypatch, config)
    env = {"Postgres_IMAGE": "ghcr.io/example/app-postgres:v1"}
    set_db_image_from_compose(config, "compose.yml", env)
    assert env["Postgres_IMAGE"] == "ghcr.io/example/app-postgres:v1"


def test_compose_that_will_not_resolve_leaves_the_old_guess(
    config: ProjectConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_compose_config(monkeypatch, config, rc=1)
    env = {"STACK_IMAGE_TAG": "my-branch"}
    set_db_image_from_compose(config, "compose.yml", env)
    assert "Postgres_IMAGE" not in env


def test_every_env_command_names_the_db_image_from_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_tooling: None
) -> None:
    """Set once, centrally, so `up`, `db configure` and `db restore` all use the same name."""
    from tests.test_dk_db_summary import _patch_env_handlers_for_restore, _restore_ns

    config, env_name = _patch_env_handlers_for_restore(tmp_path, monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(
        env_handlers,
        "set_db_image_from_compose",
        lambda _config, compose_file, env_add: calls.append(compose_file),
    )
    monkeypatch.setattr(env_handlers, "ensure_stack_matches_checkout", lambda *_a, **_k: (0, None))
    monkeypatch.setattr(stack_commands, "db_restore", lambda *_a, **_k: 0)

    assert env_handlers.handle_env_command(_restore_ns(env_name), config) == 0
    assert calls == ["compose.yml"]
