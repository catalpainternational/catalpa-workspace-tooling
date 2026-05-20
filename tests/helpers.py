"""Shared helpers for catalpa_tooling tests (not collected as tests)."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from catalpa_tooling.env_yaml import _credentials_to_env

_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "minimal_project"


def write_minimal_tooling_tree(target: Path) -> None:
    """Lay down ``tooling.yaml``, ``pyproject.toml``, and ``docker/images.yaml`` under ``target``."""
    shutil.copytree(_FIXTURE_ROOT, target, dirs_exist_ok=True)
    (target / "pyproject.toml").write_text('name = "minimal-test"\n', encoding="utf-8")
    (target / "docker").mkdir(parents=True, exist_ok=True)
    (target / "docker" / "images.yaml").write_text(
        "image_registry: ghcr.io/example/app\n",
        encoding="utf-8",
    )


def patch_module_attrs(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    attrs: dict[str, Any],
) -> None:
    """Set attributes on a module (use for functions imported with ``from … import``)."""
    for name, value in attrs.items():
        monkeypatch.setattr(f"{module}.{name}", value)


def install_in_memory_sops_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    modules: tuple[str, ...] = (
        "catalpa_tooling.sops_credentials",
        "catalpa_tooling.doctl_spaces_provision",
    ),
    apply_credential_sets: Callable[[Path, dict[str, str]], None] | None = None,
    refresh_env_credentials: Callable[[dict[str, str], Path], None] | None = None,
) -> dict[str, dict[str, str]]:
    """Mock SOPS CLI usage with an in-memory credentials store.

    Patches both ``sops_credentials`` and importing modules (e.g. ``doctl_spaces_provision``)
    because those modules bind imported names at import time.
    """
    store: dict[str, dict[str, str]] = {}

    def fake_ensure_sops_available() -> None:
        return None

    def fake_decrypt(creds_path: Path) -> dict[str, str]:
        return dict(store.get(str(creds_path), {}))

    def default_apply(creds_path: Path, values: dict[str, str]) -> None:
        store.setdefault(str(creds_path), {}).update(values)

    def default_refresh(env_add: dict[str, str], creds_path: Path) -> None:
        creds = fake_decrypt(creds_path)
        for key in list(env_add):
            if key.startswith(("PGBR_", "RESTIC_")):
                del env_add[key]
        env_add.update(_credentials_to_env(creds))

    apply_fn = apply_credential_sets or default_apply
    refresh_fn = refresh_env_credentials or default_refresh

    attrs = {
        "ensure_sops_available": fake_ensure_sops_available,
        "apply_credential_sets": apply_fn,
        "refresh_env_credentials": refresh_fn,
    }
    for mod in modules:
        patch_module_attrs(monkeypatch, mod, attrs)

    return store
