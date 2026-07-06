"""Tests for ``native.start`` parsing in tooling.yaml."""

from __future__ import annotations

import pytest

from catalpa_tooling.config import (
    DEFAULT_NATIVE_START_PORTS,
    ProjectConfigError,
    StartConfig,
    _parse_start,
)
from tests.helpers import parse_native_for_test


def test_parse_start_defaults() -> None:
    cfg = _parse_start(None)
    assert cfg == StartConfig(
        procfile=None,
        ports=DEFAULT_NATIVE_START_PORTS,
        migrate=True,
    )


def test_parse_start_jid_style() -> None:
    cfg = _parse_start({"ports": [8000, 8080]})
    assert cfg.ports == (8000, 8080)
    assert cfg.procfile is None
    assert cfg.migrate is True


def test_parse_start_custom_procfile() -> None:
    cfg = _parse_start(
        {
            "procfile": "tools/Procfile",
            "ports": [8001, 8080],
            "migrate": False,
        }
    )
    assert cfg.procfile == "tools/Procfile"
    assert cfg.ports == (8001, 8080)
    assert cfg.migrate is False


def test_parse_start_rejects_invalid_ports() -> None:
    with pytest.raises(ProjectConfigError, match="ports"):
        _parse_start({"ports": "8000"})
    with pytest.raises(ProjectConfigError, match="65535"):
        _parse_start({"ports": [0]})


def test_parse_native_includes_start() -> None:
    cfg = parse_native_for_test({"start": {"ports": [8000, 8080]}})
    assert cfg.start.ports == (8000, 8080)


def test_parse_django_port() -> None:
    from catalpa_tooling.config import DjangoDevConfig, _parse_django, native_runserver_bind

    assert _parse_django(None) == DjangoDevConfig(port=None)
    assert _parse_django({"port": 8005}) == DjangoDevConfig(port=8005)
    assert native_runserver_bind(DjangoDevConfig(port=8004)) == "0.0.0.0:8004"
    assert native_runserver_bind(DjangoDevConfig(port=None)) is None


def test_render_default_procfile_with_django_bind() -> None:
    from catalpa_tooling.native_start import render_default_procfile

    text = render_default_procfile(migrate=False, runserver_bind="0.0.0.0:8005")
    assert "runserver 0.0.0.0:8005" in text
