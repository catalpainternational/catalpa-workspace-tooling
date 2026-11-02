"""DigitalOcean account used by host storage provisioning."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from catalpa_tooling import host_storage
from catalpa_tooling.storage_config import DigitalOceanVolumeSpec, StorageVolumeSpec

_SPECS = {
    "data": StorageVolumeSpec(
        key="data",
        path="/mnt/data",
        digitalocean=DigitalOceanVolumeSpec(name="data", size_gib=10, filesystem="ext4"),
    )
}
_INFO = {"docker_host": "ssh://root@203.0.113.5"}


def _context_used(monkeypatch: pytest.MonkeyPatch, config: SimpleNamespace, **kwargs) -> list:
    seen: list = []
    monkeypatch.setattr(host_storage, "resolve_env_do_link", lambda *_a: SimpleNamespace(droplet_name="d"))

    def fake_find(_config, _link, *, context):
        seen.append(context)
        return None

    monkeypatch.setattr(host_storage, "find_droplet_for_link", fake_find)
    assert host_storage.ensure_do_block_volumes_for_specs(config, "prod", _INFO, _SPECS, **kwargs) == 1
    return seen


def test_block_volumes_use_tooling_context_when_caller_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(digitalocean=SimpleNamespace(context="personal"))
    assert _context_used(monkeypatch, config) == ["personal"]


def test_block_volumes_prefer_the_callers_context(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(digitalocean=SimpleNamespace(context="personal"))
    assert _context_used(monkeypatch, config, context="other") == ["other"]


def test_block_volumes_leave_context_unset_without_digitalocean_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _context_used(monkeypatch, SimpleNamespace(digitalocean=None)) == [None]
