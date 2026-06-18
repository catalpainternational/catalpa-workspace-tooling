"""Tests for DigitalOcean DNS helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from catalpa_tooling.doctl_domains import (
    DEFAULT_DNS_TTL,
    DnsTtlConfigError,
    do_dns_resolve_ipv4,
    env_dns_ttl,
    find_a_record,
    find_cname_record,
    hostname_to_zone_and_record_name,
    sync_host_dns,
    targets_from_site_origins,
    verify_host_dns,
)
from catalpa_tooling.doctl_projects import (
    domain_names_from_resource_urns,
    list_project_domain_urns,
)

_MINIMAL_TOOLING = """
project:
  name: test
  root_marker: tooling.yaml
paths:
  backend: django
  frontend: django
  scripts: scripts
  env_local: .env.local
  email_backend_dir: django/var/email_out
  fetch_db_dump: dump
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yaml
    dev_compose: compose.dev.yaml
    credentials_optional_envs: []
stack:
  compose_project_default: tempu
  services:
    web: django
    proxy: caddy
    db: db
  images:
    registry_key: image_registry
    components:
      web: app-web
      proxy: app-caddy
      db: app-db
  healthcheck:
    service: django
    url: http://localhost:8000/
ops:
  install_prefix: /opt/app
  config_dir: /etc/app
  systemd_unit_prefix: app-
  transfer_workdir: .transfer
  default_db_container: db-1
  pgbackrest:
    postgres_conf: pg.conf
    pgbackrest_conf: pgb.conf
    default_registry: ghcr.io/example
    restore_temp_prefix: restore_
  zabbix:
    unit_name: zabbix.service
    userparams_file: zabbix.conf
  systemd_units:
    pgbackrest: []
    restic: []
    timers_enable_pgbackrest: []
    timers_enable_restic: []
digitalocean:
  project_name: my-proj
"""


def _load_config(tmp_path: Path):
    from catalpa_tooling.config import load_project_config

    (tmp_path / "tooling.yaml").write_text(_MINIMAL_TOOLING, encoding="utf-8")
    return load_project_config(tmp_path)


def test_hostname_to_zone_apex() -> None:
    t = hostname_to_zone_and_record_name("catalpa.io", ["catalpa.io", "io"])
    assert t is not None
    assert t.zone == "catalpa.io"
    assert t.record_name == "@"


def test_hostname_to_zone_apex_subdomain_zone() -> None:
    t = hostname_to_zone_and_record_name("khs.temp.build", ["temp.build", "khs.temp.build"])
    assert t is not None
    assert t.zone == "khs.temp.build"
    assert t.record_name == "@"


def test_hostname_to_zone_www() -> None:
    t = hostname_to_zone_and_record_name("www.catalpa.io", ["catalpa.io"])
    assert t is not None
    assert t.zone == "catalpa.io"
    assert t.record_name == "www"


def test_hostname_to_zone_staging() -> None:
    t = hostname_to_zone_and_record_name(
        "staging.catalpa.io",
        ["catalpa.io", "getbero.io"],
    )
    assert t is not None
    assert t.zone == "catalpa.io"
    assert t.record_name == "staging"


def test_hostname_to_zone_longest_match() -> None:
    t = hostname_to_zone_and_record_name(
        "app.staging.catalpa.io",
        ["catalpa.io", "staging.catalpa.io"],
    )
    assert t is not None
    assert t.zone == "staging.catalpa.io"
    assert t.record_name == "app"


def test_hostname_to_zone_unknown() -> None:
    assert hostname_to_zone_and_record_name("example.com", ["catalpa.io"]) is None


def test_targets_from_site_origins() -> None:
    targets, skipped = targets_from_site_origins(
        ["staging.catalpa.io", "external.example.com"],
        ["catalpa.io"],
    )
    assert len(targets) == 1
    assert targets[0].hostname == "staging.catalpa.io"
    assert skipped == ["external.example.com"]


def test_domain_names_from_resource_urns() -> None:
    urns = ["do:domain:catalpa.io", "do:droplet:1", "do:domain:getbero.io"]
    assert domain_names_from_resource_urns(urns) == ["catalpa.io", "getbero.io"]


def test_list_project_domain_urns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.list_project_resource_urns",
        lambda _pid, *, context: [
            "do:domain:Catalpa.IO",
            "do:droplet:99",
        ],
    )
    assert list_project_domain_urns("proj-1", context=None) == {"catalpa.io"}


def test_do_dns_resolve_ipv4_via_cname(monkeypatch: pytest.MonkeyPatch) -> None:
    records_by_zone: dict[str, list[dict[str, Any]]] = {
        "catalpa.io": [
            {"type": "A", "name": "@", "data": "203.0.113.5"},
            {"type": "CNAME", "name": "www", "data": "catalpa.io."},
        ],
    }

    def fake_list(zone: str, *, context: str | None) -> list[dict[str, Any]]:
        return records_by_zone.get(zone, [])

    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_domain_records",
        fake_list,
    )
    assert (
        do_dns_resolve_ipv4("www.catalpa.io", ["catalpa.io"], context=None)
        == "203.0.113.5"
    )
    assert find_cname_record("catalpa.io", "www", context=None) is not None


def test_do_dns_resolve_ipv4_cname_to_at_apex(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        {"type": "A", "name": "@", "data": "203.0.113.5"},
        {"type": "CNAME", "name": "www", "data": "@"},
    ]

    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_domain_records",
        lambda _zone, *, context: records,
    )
    assert (
        do_dns_resolve_ipv4("www.catalpa.io", ["catalpa.io"], context=None)
        == "203.0.113.5"
    )


def test_find_a_record_matches_apex_at(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        {"id": 1, "type": "A", "name": "@", "data": "1.2.3.4"},
        {"id": 2, "type": "A", "name": "www", "data": "1.2.3.4"},
    ]

    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_domain_records",
        lambda _zone, *, context: records,
    )
    found = find_a_record("catalpa.io", "@", context=None)
    assert found is not None
    assert found["data"] == "1.2.3.4"


def _patch_dns_api(monkeypatch: pytest.MonkeyPatch, *, record_ip: str) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_registered_domains",
        lambda *, context: ["catalpa.io"],
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.resolve_project_id",
        lambda *_a, **_k: "proj-uuid",
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.list_project_domain_urns",
        lambda *_a, **_k: {"catalpa.io"},
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.find_a_record",
        lambda zone, name, *, context: {
            "id": 9,
            "type": "A",
            "name": name,
            "data": record_ip,
        },
    )


def test_verify_host_dns_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _load_config(tmp_path)
    _patch_dns_api(monkeypatch, record_ip="203.0.113.5")
    info = {"site_origin": ["staging.catalpa.io"]}
    assert verify_host_dns(config, info, droplet_ip="203.0.113.5", context=None) == 0


def test_verify_host_dns_wrong_ip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _load_config(tmp_path)
    _patch_dns_api(monkeypatch, record_ip="198.51.100.1")
    info = {"site_origin": ["staging.catalpa.io"]}
    assert verify_host_dns(config, info, droplet_ip="203.0.113.5", context=None) == 1


def test_verify_host_dns_www_cname_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config = _load_config(tmp_path)
    records_by_zone: dict[str, list[dict[str, Any]]] = {
        "catalpa.io": [
            {"type": "A", "name": "@", "data": "203.0.113.5"},
            {"type": "CNAME", "name": "www", "data": "catalpa.io."},
        ],
    }

    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_registered_domains",
        lambda *, context: ["catalpa.io"],
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.resolve_project_id",
        lambda *_a, **_k: "proj-uuid",
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.list_project_domain_urns",
        lambda *_a, **_k: {"catalpa.io"},
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_domain_records",
        lambda zone, *, context: records_by_zone.get(zone, []),
    )
    info = {"site_origin": ["www.catalpa.io"]}
    assert verify_host_dns(config, info, droplet_ip="203.0.113.5", context=None) == 0
    assert "203.0.113.5" in capsys.readouterr().err


def test_verify_host_dns_www_cname_to_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DO often uses ``@`` as CNAME rdata for the zone apex."""
    config = _load_config(tmp_path)
    records_by_zone: dict[str, list[dict[str, Any]]] = {
        "catalpa.io": [
            {"type": "A", "name": "@", "data": "203.0.113.5"},
            {"type": "CNAME", "name": "www", "data": "@"},
        ],
    }

    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_registered_domains",
        lambda *, context: ["catalpa.io"],
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.resolve_project_id",
        lambda *_a, **_k: "proj-uuid",
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.list_project_domain_urns",
        lambda *_a, **_k: {"catalpa.io"},
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_domain_records",
        lambda zone, *, context: records_by_zone.get(zone, []),
    )
    info = {"site_origin": ["www.catalpa.io"]}
    assert verify_host_dns(config, info, droplet_ip="203.0.113.5", context=None) == 0


def test_verify_host_dns_skips_external(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config = _load_config(tmp_path)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_registered_domains",
        lambda *, context: ["catalpa.io"],
    )
    info = {"site_origin": ["not-on-do.example.com"]}
    assert verify_host_dns(config, info, droplet_ip="203.0.113.5", context=None) == 0
    assert "skipping DNS check" in capsys.readouterr().err


def test_env_dns_ttl_default() -> None:
    assert env_dns_ttl({}) == DEFAULT_DNS_TTL
    assert env_dns_ttl({"digitalocean": {}}) == DEFAULT_DNS_TTL


def test_env_dns_ttl_from_info() -> None:
    assert env_dns_ttl({"digitalocean": {"dns_ttl": 3600}}) == 3600


def test_env_dns_ttl_rejects_invalid() -> None:
    with pytest.raises(DnsTtlConfigError, match="integer"):
        env_dns_ttl({"digitalocean": {"dns_ttl": "300"}})
    with pytest.raises(DnsTtlConfigError, match="between"):
        env_dns_ttl({"digitalocean": {"dns_ttl": 10}})


def test_sync_host_dns_skips_cname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config = _load_config(tmp_path)
    records = [
        {"type": "A", "name": "@", "data": "203.0.113.5"},
        {"type": "CNAME", "name": "www", "data": "catalpa.io."},
    ]

    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_registered_domains",
        lambda *, context: ["catalpa.io"],
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_domain_records",
        lambda _zone, *, context: records,
    )
    run_calls: list[list[str]] = []

    def fake_run(args: list[str], *, context: str | None, **kwargs: Any) -> Any:
        run_calls.append(list(args))
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl", fake_run)
    info = {"site_origin": ["www.catalpa.io"]}
    assert sync_host_dns(
        config, info, droplet_ip="203.0.113.5", context=None, dry_run=False
    ) == 0
    err = capsys.readouterr().err
    assert "uses CNAME" in err
    assert run_calls == []


def test_sync_host_dns_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config = _load_config(tmp_path)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_registered_domains",
        lambda *, context: ["catalpa.io"],
    )
    created: list[list[str]] = []

    def fake_run(args: list[str], *, context: str | None, **kwargs: Any) -> Any:
        created.append(list(args))
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("catalpa_tooling.doctl_domains.find_a_record", lambda *a, **k: None)
    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl", fake_run)
    info = {"site_origin": ["staging.catalpa.io"]}
    assert sync_host_dns(
        config, info, droplet_ip="203.0.113.5", context=None, dry_run=True
    ) == 0
    err = capsys.readouterr().err
    assert "dry-run" in err
    assert "--record-ttl" in err
    assert str(DEFAULT_DNS_TTL) in err


def test_sync_host_dns_uses_env_dns_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config = _load_config(tmp_path)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_registered_domains",
        lambda *, context: ["catalpa.io"],
    )
    monkeypatch.setattr("catalpa_tooling.doctl_domains.find_a_record", lambda *a, **k: None)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.run_doctl",
        lambda *a, **k: __import__("types").SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    info = {
        "site_origin": ["staging.catalpa.io"],
        "digitalocean": {"dns_ttl": 3600},
    }
    assert sync_host_dns(
        config, info, droplet_ip="203.0.113.5", context=None, dry_run=True
    ) == 0
    assert "3600" in capsys.readouterr().err


def test_sync_host_dns_invalid_ttl_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config = _load_config(tmp_path)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_registered_domains",
        lambda *, context: ["catalpa.io"],
    )
    info = {"site_origin": ["staging.catalpa.io"], "digitalocean": {"dns_ttl": 5}}
    assert sync_host_dns(
        config, info, droplet_ip="203.0.113.5", context=None, dry_run=True
    ) == 1
    assert "dns_ttl" in capsys.readouterr().err.lower()


def test_sync_host_dns_apex_uses_at_record_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config = _load_config(tmp_path)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_registered_domains",
        lambda *, context: ["khs.temp.build"],
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.find_a_record_exact",
        lambda *a, **k: None,
    )
    monkeypatch.setattr("catalpa_tooling.doctl_domains.find_a_record", lambda *a, **k: None)
    monkeypatch.setattr("catalpa_tooling.doctl_domains.find_cname_record", lambda *a, **k: None)
    created: list[list[str]] = []

    def fake_run(args: list[str], *, context: str | None, **kwargs: Any) -> Any:
        created.append(list(args))
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl", fake_run)
    info = {"site_origin": ["https://khs.temp.build"]}
    assert sync_host_dns(
        config, info, droplet_ip="178.128.109.40", context=None, dry_run=False
    ) == 0
    assert created
    assert "--record-name" in created[0]
    assert created[0][created[0].index("--record-name") + 1] == "@"


def test_sync_host_dns_removes_malformed_apex_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    config = _load_config(tmp_path)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.list_registered_domains",
        lambda *, context: ["khs.temp.build"],
    )
    malformed = {"id": 42, "type": "A", "name": "khs.temp.build", "data": "178.128.109.40"}
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.find_a_record_exact",
        lambda zone, name, *, context: malformed if name == zone else None,
    )
    monkeypatch.setattr("catalpa_tooling.doctl_domains.find_a_record", lambda *a, **k: None)
    monkeypatch.setattr("catalpa_tooling.doctl_domains.find_cname_record", lambda *a, **k: None)
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, context: str | None, **kwargs: Any) -> Any:
        calls.append(list(args))
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl", fake_run)
    info = {"site_origin": ["https://khs.temp.build"]}
    assert sync_host_dns(
        config, info, droplet_ip="178.128.109.40", context=None, dry_run=False
    ) == 0
    err = capsys.readouterr().err
    assert "Removing malformed apex A record" in err
    assert calls[0][0:4] == ["compute", "domain", "records", "delete"]
    assert calls[1][calls[1].index("--record-name") + 1] == "@"
