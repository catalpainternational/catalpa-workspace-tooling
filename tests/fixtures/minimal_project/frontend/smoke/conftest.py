"""Shared fixtures for minimal_project smoke tests (reference for docs/SMOKE_TESTS.md)."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def browser_type_launch_args() -> dict:
    return {"headless": True}


@pytest.fixture
def fe_url() -> str:
    url = (os.environ.get("SMOKE_FE_URL") or "").strip()
    if not url:
        pytest.skip("SMOKE_FE_URL not set (run via `uv run tests smoke`)")
    return url.rstrip("/")
