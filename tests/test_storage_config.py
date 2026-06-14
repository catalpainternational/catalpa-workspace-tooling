"""Tests for storage_config and host-bound Docker volumes."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from catalpa_tooling.pgbackrest_volume_config import (
    _volume_bind_host_path,
    ensure_external_stack_volumes,
    stack_volume_docker_name,
)
from catalpa_tooling.storage_config import (
    default_do_volume_name,
    parse_storage_volumes_from_info,
    sanitize_do_volume_name,
    StorageConfigError,
)


def test_parse_path_only(minimal_project) -> None:
    specs = parse_storage_volumes_from_info(
        {
            "storage": {
                "volumes": {
                    "django_media": {"path": "/mnt/media"},
                }
            }
        },
        minimal_project,
    )
    assert len(specs) == 1
    spec = specs["django_media"]
    assert spec.path == "/mnt/media"
    assert spec.digitalocean is None


def test_sanitize_do_volume_name_replaces_underscores() -> None:
    assert sanitize_do_volume_name("jid-prod-django_media") == "jid-prod-django-media"


def test_default_do_volume_name_sanitizes_compose_key() -> None:
    assert (
        default_do_volume_name(
            "django_media",
            droplet_name="jid-prod",
        )
        == "jid-prod-django-media"
    )


def test_parse_with_digitalocean(minimal_project) -> None:
    specs = parse_storage_volumes_from_info(
        {
            "storage": {
                "volumes": {
                    "django_media": {
                        "path": "/mnt/jid-media",
                        "digitalocean": {"size_gib": 100},
                    }
                }
            }
        },
        minimal_project,
    )
    do = specs["django_media"].digitalocean
    assert do is not None
    assert do.size_gib == 100


def test_invalid_key(minimal_project) -> None:
    with pytest.raises(StorageConfigError):
        parse_storage_volumes_from_info(
            {"storage": {"volumes": {"not_a_volume": {"path": "/mnt/x"}}}},
            minimal_project,
        )


def test_relative_path_rejected(minimal_project) -> None:
    with pytest.raises(StorageConfigError):
        parse_storage_volumes_from_info(
            {"storage": {"volumes": {"django_media": {"path": "mnt/media"}}}},
            minimal_project,
        )


def test_volume_bind_host_path_reads_bind_device() -> None:
    entry = {
        "Options": {
            "device": "/mnt/jid-media",
            "o": "bind",
            "type": "none",
        }
    }
    assert _volume_bind_host_path(entry) == "/mnt/jid-media"


def test_volume_bind_host_path_non_bind_returns_none() -> None:
    assert _volume_bind_host_path({"Options": {}}) is None


def test_creates_bind_volume(minimal_project) -> None:
    env = {"COMPOSE_PROJECT_NAME": "testproj"}
    vol_name = stack_volume_docker_name(env, "django_media", config=minimal_project)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[0] == "ssh":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("catalpa_tooling.pgbackrest_volume_config.run_cmd", side_effect=fake_run):
        rc = ensure_external_stack_volumes(
            env,
            config=minimal_project,
            volume_hosts={"django_media": "/mnt/media"},
            create_host_paths={},
        )
    assert rc == 0
    create_cmds = [c for c in calls if c[:3] == ["docker", "volume", "create"]]
    assert any(vol_name in c for c in create_cmds)
    bind_create = next(c for c in create_cmds if vol_name in c)
    assert "device=/mnt/media" in bind_create


def test_mismatch_existing_volume_fails(minimal_project) -> None:
    env = {"COMPOSE_PROJECT_NAME": "testproj"}
    inspect_payload = json.dumps(
        [{"Options": {"device": "/other/path", "o": "bind"}}]
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "ssh":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, inspect_payload, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("catalpa_tooling.pgbackrest_volume_config.run_cmd", side_effect=fake_run):
        rc = ensure_external_stack_volumes(
            env,
            config=minimal_project,
            volume_hosts={"django_media": "/mnt/media"},
        )
    assert rc == 1
