"""Tests for DigitalOcean project and droplet helpers."""

from __future__ import annotations

import json
from typing import Any

import pytest

from catalpa_tooling.config import DigitalOceanConfig
from catalpa_tooling.doctl_projects import (
    domain_names_from_resource_urns,
    droplet_ids_from_resource_urns,
    find_project_droplet_id_by_name,
    list_project_domain_urns,
    list_project_droplets,
    resolve_project_id,
)


def test_domain_names_from_resource_urns() -> None:
    assert domain_names_from_resource_urns(
        ["do:domain:example.com", "do:droplet:1"]
    ) == ["example.com"]


def test_list_project_domain_urns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.list_project_resource_urns",
        lambda _pid, *, context: ["do:domain:Example.COM"],
    )
    assert list_project_domain_urns("p", context=None) == {"example.com"}


def test_droplet_ids_from_resource_urns() -> None:
    urns = [
        "do:droplet:111",
        "do:dbaas:abc",
        "do:droplet:222",
        "bad",
    ]
    assert droplet_ids_from_resource_urns(urns) == [111, 222]


def test_resolve_project_id_uuid_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects._projects_list",
        lambda *, context: pytest.fail("should not list projects"),
    )
    assert resolve_project_id(uuid, do_config=None, context=None) == uuid


def test_resolve_project_id_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_list(*, context: str | None) -> list[dict[str, Any]]:
        assert context == "team"
        return [{"id": "proj-uuid", "name": "My App"}]

    monkeypatch.setattr("catalpa_tooling.doctl_projects._projects_list", fake_list)
    assert (
        resolve_project_id("my app", do_config=None, context="team") == "proj-uuid"
    )


def test_resolve_project_id_from_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects._projects_list",
        lambda *, context: [{"id": "from-yaml", "name": "staging"}],
    )
    do = DigitalOceanConfig(
        project_name="staging",
        project_id=None,
        context=None,
        timezone=None,
        region=None,
        size=None,
        image=None,
        ssh_keys=None,
    )
    assert resolve_project_id(None, do_config=do, context=None) == "from-yaml"


def test_resolve_project_id_missing_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as exc:
        resolve_project_id(None, do_config=None, context=None)
    assert exc.value.code == 1


def test_list_project_droplets_empty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.list_project_resource_urns",
        lambda _pid, *, context: [],
    )
    assert list_project_droplets("proj-id", context=None) == 0
    assert "No droplets" in capsys.readouterr().out


def test_list_project_droplets_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.list_project_resource_urns",
        lambda _pid, *, context: ["do:droplet:10"],
    )

    def fake_json(args: list[str], *, context: str | None) -> object:
        assert args[:3] == ["compute", "droplet", "list"]
        return [
            {"id": 10, "name": "web-1", "status": "active", "networks": {"v4": []}, "region": {"slug": "nyc1"}},
            {"id": 99, "name": "other", "status": "active"},
        ]

    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl_json", fake_json)
    assert list_project_droplets("proj-id", context=None, as_json=True) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["id"] == 10


def test_find_project_droplet_id_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.list_project_resource_urns",
        lambda _pid, *, context: ["do:droplet:10", "do:droplet:20"],
    )

    def fake_json(args: list[str], *, context: str | None) -> object:
        return [
            {"id": 10, "name": "tempu-test"},
            {"id": 20, "name": "other"},
        ]

    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl_json", fake_json)
    assert find_project_droplet_id_by_name("proj-id", "tempu-test", context=None) == 10
    assert find_project_droplet_id_by_name("proj-id", "TEMPu-Test", context=None) == 10
    assert find_project_droplet_id_by_name("proj-id", "missing", context=None) is None
