"""Tests for ``native.frontend`` parsing in tooling.yaml."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalpa_tooling.config import (
    FrontendDevConfig,
    ProjectConfigError,
    _parse_frontend,
    resolve_frontend_package_manager,
)
from tests.helpers import parse_native_for_test


def test_parse_frontend_defaults() -> None:
    cfg = _parse_frontend(None)
    assert cfg == FrontendDevConfig(
        package_manager=None,
        script="dev",
        install=True,
        env={},
        node_version=None,
    )


def test_parse_frontend_bero_style() -> None:
    cfg = _parse_frontend(
        {
            "package_manager": "yarn",
            "script": "start",
            "env": {
                "WEBPACK_DEVSERVER_PROXY_TARGET": "http://127.0.0.1:8000",
                "SKIP_SW": "true",
            },
        }
    )
    assert cfg.package_manager == "yarn"
    assert cfg.script == "start"
    assert cfg.install is True
    assert cfg.env["WEBPACK_DEVSERVER_PROXY_TARGET"] == "http://127.0.0.1:8000"
    assert cfg.env["SKIP_SW"] == "true"


def test_parse_frontend_install_false() -> None:
    cfg = _parse_frontend({"install": False})
    assert cfg.install is False


def test_parse_frontend_rejects_invalid_package_manager() -> None:
    with pytest.raises(ProjectConfigError, match="package_manager"):
        _parse_frontend({"package_manager": "bun"})


def test_parse_frontend_node_version() -> None:
    cfg = _parse_frontend({"node_version": "22"})
    assert cfg.node_version == "22"


def test_parse_native_includes_frontend() -> None:
    cfg = parse_native_for_test({"frontend": {"script": "start", "package_manager": "yarn"}})
    assert cfg.frontend.script == "start"
    assert cfg.frontend.package_manager == "yarn"


def test_resolve_package_manager_from_package_json(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps({"packageManager": "yarn@4.9.2"}),
        encoding="utf-8",
    )
    assert resolve_frontend_package_manager(frontend, configured=None) == "yarn"


def test_resolve_package_manager_from_yarn_lock(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}", encoding="utf-8")
    (frontend / "yarn.lock").write_text("", encoding="utf-8")
    assert resolve_frontend_package_manager(frontend, configured=None) == "yarn"


def test_resolve_package_manager_explicit_override(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps({"packageManager": "yarn@4.9.2"}),
        encoding="utf-8",
    )
    assert resolve_frontend_package_manager(frontend, configured="npm") == "npm"


def test_resolve_package_manager_defaults_to_npm(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text("{}", encoding="utf-8")
    assert resolve_frontend_package_manager(frontend, configured=None) == "npm"
