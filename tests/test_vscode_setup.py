"""Tests for setup-vscode / vscode_setup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalpa_tooling.vscode_setup import (
    apply_remove,
    apply_setup,
    inspect_status,
    plan_remove,
    plan_setup,
)
from catalpa_tooling.vscode_tasks import (
    CURSOR_BROWSER_COMMAND,
    DEV_CURSOR_BROWSER_INPUT,
    FULL_CURSOR_BROWSER_INPUT,
    MANAGED_MARKER_KEY,
    WorkflowKind,
)

_MINIMAL_OPS = """
  pgbackrest:
    postgres_conf: a.conf
    pgbackrest_conf: b.conf
    default_registry: reg
  zabbix:
    unit_name: u.service
    userparams_file: 99.conf
  systemd_units:
    pgbackrest: []
    restic: []
"""


def _write_tooling_docker_repo(repo: Path, *, include_full: bool = True) -> None:
    (repo / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    (repo / "tooling.yaml").write_text(
        """
project:
  name: test-docker
  root_marker: pyproject.toml
paths:
  backend: .
  frontend: .
  scripts: scripts
  env_local: .env.local
  email_backend_dir: var/email
  fetch_db_dump: docker/postgres/dumps/db.custom
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yaml
    dev_compose: compose.dev.yaml
stack:
  compose_project_default: test
  services:
    web: django
    proxy: caddy
    db: db
  images:
    registry_key: image_registry
    components:
      web: test-django
      proxy: test-caddy
      db: test-db
  healthcheck:
    service: django
    url: http://127.0.0.1:8000/
ops:
  install_prefix: /opt/test
  config_dir: /etc/test
  systemd_unit_prefix: test-
  transfer_workdir: .transfer
  default_db_container: test_db_1
{_MINIMAL_OPS}
""".format(_MINIMAL_OPS=_MINIMAL_OPS),
        encoding="utf-8",
    )
    (repo / "docker").mkdir()
    (repo / "docker" / "images.yaml").write_text("image_registry: test\n", encoding="utf-8")
    dev_info = repo / "docker/envs/dev"
    dev_info.mkdir(parents=True)
    (dev_info / "info.yaml").write_text(
        "site_origin: http://localhost:9001\ncompose_file: compose.dev.yaml\n",
        encoding="utf-8",
    )
    if include_full:
        full_info = repo / "docker/envs/full"
        full_info.mkdir(parents=True)
        (full_info / "info.yaml").write_text(
            "site_origin: https://bero.localhost:9011\ncompose_file: compose.yaml\n",
            encoding="utf-8",
        )


def test_plan_setup_docker_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_tooling_docker_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    plan = plan_setup()
    assert plan.workflow == WorkflowKind.DOCKER
    assert plan.write_tasks
    assert "uv run dk dev up -d" in plan.tasks_content
    assert "uv run dk full up -d" in plan.tasks_content
    assert "native fetch" not in plan.tasks_content
    assert "native start" not in plan.tasks_content
    assert "uv run dk dev -y db restore" in plan.tasks_content
    assert "uv run dk dev -y files restore" in plan.tasks_content


def test_dev_trust_task_when_local_proxy_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tooling_docker_repo(tmp_path)
    (tmp_path / "docker/envs/dev/info.yaml").write_text(
        "site_origin: https://test-dev.localdev.temp.build\n"
        "compose_file: compose.dev.yaml\n"
        "local_proxy:\n"
        "  enabled: true\n"
        "  upstream_port: 5555\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    plan = plan_setup()
    labels = [t["label"] for t in json.loads(plan.tasks_content)["tasks"]]
    assert "Trust Catalpa local dev CA" in labels
    assert "uv run dk dev trust-caddy-cert" in plan.tasks_content


def test_plan_setup_dev_only_when_no_full_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tooling_docker_repo(tmp_path, include_full=False)
    monkeypatch.chdir(tmp_path)

    plan = plan_setup()
    assert "uv run dk dev up -d" in plan.tasks_content
    assert "uv run dk full up -d" not in plan.tasks_content


def test_apply_setup_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_tooling_docker_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".vscode/\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    first = plan_setup()
    apply_setup(first)
    status_after = inspect_status()
    assert status_after.ready

    second = plan_setup()
    assert not second.write_tasks
    assert not second.write_extensions
    assert not second.write_settings
    assert not second.patch_gitignore


def test_gitignore_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_tooling_docker_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("# IDE\n.vscode/\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    plan = plan_setup()
    assert plan.patch_gitignore
    apply_setup(plan)

    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "!.vscode/tasks.json" in text
    assert ".vscode/*" in text


def test_remove_managed_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_tooling_docker_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    apply_setup(plan_setup())
    tasks_path = tmp_path / ".vscode/tasks.json"
    assert tasks_path.is_file()

    remove_plan = plan_remove()
    apply_remove(remove_plan)
    assert not tasks_path.is_file()


def test_tasks_json_has_managed_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_tooling_docker_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    apply_setup(plan_setup())
    data = json.loads((tmp_path / ".vscode/tasks.json").read_text(encoding="utf-8"))
    assert data[MANAGED_MARKER_KEY] == "7"
    labels = [t["label"] for t in data["tasks"]]
    assert "Dev: Show LAN URLs" in labels
    assert "Dev: Open site on LAN" in labels
    assert "Dev: Open site in Cursor browser" in labels
    assert "Full: Open site in Cursor browser" in labels
    # Default-on local proxy for local dev envs -> trust task is included.
    assert "Trust Catalpa local dev CA" in labels

    dev_cursor_task = next(
        t for t in data["tasks"] if t["label"] == "Dev: Open site in Cursor browser"
    )
    assert dev_cursor_task["type"] == "shell"
    assert dev_cursor_task["command"] == f"echo ${{input:{DEV_CURSOR_BROWSER_INPUT}}}"
    assert dev_cursor_task["presentation"]["reveal"] == "never"
    assert dev_cursor_task["presentation"]["echo"] is False

    dev_input = next(i for i in data["inputs"] if i["id"] == DEV_CURSOR_BROWSER_INPUT)
    assert dev_input["type"] == "command"
    assert dev_input["command"] == CURSOR_BROWSER_COMMAND
    assert dev_input["args"] == {"url": "http://localhost:9001"}

    full_input = next(i for i in data["inputs"] if i["id"] == FULL_CURSOR_BROWSER_INPUT)
    assert full_input["args"] == {"url": "https://bero.localhost:9011"}
