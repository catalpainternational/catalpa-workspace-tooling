"""Tests for DigitalOcean ↔ dk environment linking."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from catalpa_tooling.deploy_do_link import (
    EnvDoLink,
    default_droplet_name,
    droplet_name_for_env,
    droplet_name_to_env_map,
    format_docker_host,
    is_digitalocean_host_disabled,
    patch_info_docker_host,
    public_ipv4,
    read_env_do_link,
    resolve_env_do_link,
)
from catalpa_tooling.doctl_projects import _droplet_row


def test_public_ipv4() -> None:
    droplet = {
        "networks": {
            "v4": [
                {"type": "private", "ip_address": "10.0.0.1"},
                {"type": "public", "ip_address": "203.0.113.5"},
            ]
        }
    }
    assert public_ipv4(droplet) == "203.0.113.5"


def test_is_digitalocean_host_disabled() -> None:
    assert is_digitalocean_host_disabled({}) is False
    assert is_digitalocean_host_disabled({"digitalocean": {}}) is False
    assert is_digitalocean_host_disabled({"digitalocean": {"disabled": True}}) is True
    assert is_digitalocean_host_disabled({"digitalocean": {"disabled": "yes"}}) is True


def test_read_env_do_link() -> None:
    assert read_env_do_link({}) is None
    assert read_env_do_link({"digitalocean": {}}) is None
    link = read_env_do_link({"digitalocean": {"droplet_name": "web-1", "ssh_user": "deploy"}})
    assert link == EnvDoLink(
        droplet_name="web-1",
        ssh_user="deploy",
        explicit_droplet_name=True,
        size=None,
        region=None,
    )
    link = read_env_do_link(
        {
            "digitalocean": {
                "droplet_name": "web-1",
                "size": "s-1vcpu-2gb",
                "region": "sgp1",
            }
        }
    )
    assert link == EnvDoLink(
        droplet_name="web-1",
        ssh_user="root",
        explicit_droplet_name=True,
        size="s-1vcpu-2gb",
        region="sgp1",
    )


def test_format_docker_host() -> None:
    assert format_docker_host("root", "1.2.3.4") == "ssh://root@1.2.3.4"
    assert format_docker_host("", "1.2.3.4") == "ssh://root@1.2.3.4"


def test_droplet_row_env_column() -> None:
    droplet = {
        "id": 1,
        "name": "marktwain-prod",
        "status": "active",
        "region": {"slug": "sgp1"},
        "networks": {"v4": [{"type": "public", "ip_address": "1.2.3.4"}]},
    }
    row = _droplet_row(
        droplet,
        ("Name", "Env"),
        env_by_droplet_name={"marktwain-prod": "prod"},
    )
    assert row == ["marktwain-prod", "prod"]


def test_patch_info_docker_host(tmp_path: Path) -> None:
    info_path = tmp_path / "info.yaml"
    info_path.write_text("site_origin: example.com\n", encoding="utf-8")
    assert patch_info_docker_host(info_path, "ssh://root@1.2.3.4", dry_run=True) == 0
    data = yaml.safe_load(info_path.read_text(encoding="utf-8"))
    assert "docker_host" not in data

    assert patch_info_docker_host(info_path, "ssh://root@1.2.3.4", dry_run=False) == 0
    data = yaml.safe_load(info_path.read_text(encoding="utf-8"))
    assert data["docker_host"] == "ssh://root@1.2.3.4"


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
"""


def _load_test_config(tmp_path: Path):
    from catalpa_tooling.config import load_project_config

    (tmp_path / "tooling.yaml").write_text(_MINIMAL_TOOLING, encoding="utf-8")
    return load_project_config(tmp_path)


def _active_droplet(name: str, ip: str = "203.0.113.5") -> dict:
    return {
        "id": 1,
        "name": name,
        "status": "active",
        "region": {"slug": "sgp1"},
        "networks": {"v4": [{"type": "public", "ip_address": ip}]},
    }


def test_default_droplet_name(minimal_project) -> None:
    assert default_droplet_name(minimal_project, "prod") == "minimal-prod"


def test_resolve_env_do_link_default_and_explicit(minimal_project) -> None:
    default = resolve_env_do_link(minimal_project, "staging", {})
    assert default == EnvDoLink(
        droplet_name="minimal-staging",
        ssh_user="root",
        explicit_droplet_name=False,
        size=None,
        region=None,
    )
    explicit = resolve_env_do_link(
        minimal_project,
        "staging",
        {"digitalocean": {"droplet_name": "custom", "ssh_user": "deploy"}},
    )
    assert explicit == EnvDoLink(
        droplet_name="custom",
        ssh_user="deploy",
        explicit_droplet_name=True,
        size=None,
        region=None,
    )


def test_resolve_env_do_link_size_region_without_droplet_name(minimal_project) -> None:
    link = resolve_env_do_link(
        minimal_project,
        "staging",
        {"digitalocean": {"size": "s-1vcpu-2gb", "region": "sgp1"}},
    )
    assert link == EnvDoLink(
        droplet_name="minimal-staging",
        ssh_user="root",
        explicit_droplet_name=False,
        size="s-1vcpu-2gb",
        region="sgp1",
    )


def test_droplet_name_to_env_map_includes_default_names(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("description: test\n", encoding="utf-8")
    mapping = droplet_name_to_env_map(config)
    assert mapping["test-prod"] == "prod"


def test_droplet_name_for_env_without_explicit_override(tmp_path: Path) -> None:
    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("description: test\n", encoding="utf-8")
    assert droplet_name_for_env(config, "prod") == "test-prod"


def test_cmd_env_host_no_doctl_prints_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        "docker_host: ssh://root@203.0.113.5\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.try_resolve_doctl_binary", lambda: None
    )
    assert cmd_env_host(config, "prod", write=False) == 0
    assert "docker_host: ssh://root@203.0.113.5" in capsys.readouterr().out


def test_cmd_env_host_no_doctl_missing_docker_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("description: test\n", encoding="utf-8")
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.try_resolve_doctl_binary", lambda: None
    )
    assert cmd_env_host(config, "prod", write=False) == 1


def test_cmd_env_host_write_without_doctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        "docker_host: ssh://root@1.2.3.4\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.try_resolve_doctl_binary", lambda: None
    )
    assert cmd_env_host(config, "prod", write=True) == 1


def test_cmd_env_host_inactive_droplet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("description: test\n", encoding="utf-8")
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.try_resolve_doctl_binary", lambda: Path("/doctl")
    )

    def fake_find(*_a, **_k):
        d = _active_droplet("test-prod")
        d["status"] = "off"
        return d

    monkeypatch.setattr("catalpa_tooling.deploy_do_link.find_droplet_for_link", fake_find)
    assert cmd_env_host(config, "prod", write=False) == 1


def test_cmd_env_host_active_droplet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("description: test\n", encoding="utf-8")
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.try_resolve_doctl_binary", lambda: Path("/doctl")
    )
    monkeypatch.setattr(
        "catalpa_tooling.deploy_do_link.find_droplet_for_link",
        lambda *_a, **_k: _active_droplet("test-prod"),
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.verify_host_dns",
        lambda *_a, **_k: 0,
    )
    assert cmd_env_host(config, "prod", write=False) == 0
    assert "docker_host: ssh://root@203.0.113.5" in capsys.readouterr().out


def test_cmd_env_host_write_registers_known_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("description: test\n", encoding="utf-8")
    ensure_calls: list[tuple[str, dict]] = []

    def fake_ensure(dh: str, **kw: object) -> int:
        ensure_calls.append((dh, kw))
        return 0

    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.try_resolve_doctl_binary", lambda: Path("/doctl")
    )
    monkeypatch.setattr(
        "catalpa_tooling.deploy_do_link.find_droplet_for_link",
        lambda *_a, **_k: _active_droplet("test-prod"),
    )
    monkeypatch.setattr(
        "catalpa_tooling.ssh_known_hosts.ensure_ssh_known_host_for_docker_host",
        fake_ensure,
    )
    assert cmd_env_host(config, "prod", write=True, verify_dns=False) == 0
    assert ensure_calls == [("ssh://root@203.0.113.5", {"dry_run": False})]
    data = yaml.safe_load((env_dir / "info.yaml").read_text(encoding="utf-8"))
    assert data["docker_host"] == "ssh://root@203.0.113.5"


def test_cmd_env_host_calls_verify_dns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        "site_origin:\n  - staging.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.try_resolve_doctl_binary", lambda: Path("/doctl")
    )
    monkeypatch.setattr(
        "catalpa_tooling.deploy_do_link.find_droplet_for_link",
        lambda *_a, **_k: _active_droplet("test-prod"),
    )
    do_calls: list[str] = []
    public_calls: list[str] = []

    def fake_do_verify(_config, _info, *, droplet_ip: str, context: str | None, **kwargs) -> int:
        do_calls.append(droplet_ip)
        return 0

    def fake_public_verify(_info, expected_ip: str) -> int:
        public_calls.append(expected_ip)
        return 0

    monkeypatch.setattr("catalpa_tooling.doctl_domains.verify_host_dns", fake_do_verify)
    monkeypatch.setattr(
        "catalpa_tooling.dns_resolve.verify_public_dns_from_info", fake_public_verify
    )
    assert cmd_env_host(config, "prod", write=False) == 0
    assert do_calls == ["203.0.113.5"]
    assert public_calls == ["203.0.113.5"]


def test_cmd_env_host_dry_run_uses_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("description: test\n", encoding="utf-8")
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.try_resolve_doctl_binary", lambda: Path("/doctl")
    )
    called: list[bool] = []

    def fake_find(*_a, **_k):
        called.append(True)
        return _active_droplet("test-prod")

    monkeypatch.setattr("catalpa_tooling.deploy_do_link.find_droplet_for_link", fake_find)
    assert cmd_env_host(config, "prod", write=False, dry_run=True) == 0
    assert called


def test_find_droplet_for_link_project_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling.deploy_do_link import EnvDoLink, find_droplet_for_link

    config = _load_test_config(tmp_path)
    tooling_path = tmp_path / "tooling.yaml"
    data = yaml.safe_load(tooling_path.read_text(encoding="utf-8"))
    data["digitalocean"] = {"project_name": "my-proj"}
    tooling_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    from catalpa_tooling.config import load_project_config

    config = load_project_config(tmp_path)
    link = EnvDoLink(droplet_name="test-prod")
    project_calls: list[str] = []

    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.resolve_project_id",
        lambda *_a, **_k: (project_calls.append("resolve") or "proj-1"),
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.find_project_droplet_by_name",
        lambda pid, name, *, context: _active_droplet(name),
    )
    found = find_droplet_for_link(config, link, context=None)
    assert found is not None
    assert project_calls == ["resolve"]


def test_find_droplet_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from catalpa_tooling.deploy_do_link import find_droplet_by_name

    def fake_json(args: list[str], *, context: str | None) -> object:
        return [
            {"id": 1, "name": "alpha"},
            {"id": 2, "name": "marktwain-prod"},
        ]

    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl_json", fake_json)
    found = find_droplet_by_name("marktwain-prod", context=None)
    assert found is not None
    assert found["id"] == 2
    assert find_droplet_by_name("missing", context=None) is None


def test_cmd_env_host_disabled_skips_droplet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "staging"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        "docker_host: ssh://root@203.0.113.5\n"
        "site_origin:\n  - staging.example.com\n"
        "digitalocean:\n  disabled: true\n",
        encoding="utf-8",
    )
    find_called = False

    def fake_find(*_a, **_k):
        nonlocal find_called
        find_called = True
        return _active_droplet("test-staging")

    public_calls: list[str] = []

    def fake_public(_info, expected_ip: str) -> int:
        public_calls.append(expected_ip)
        return 0

    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.try_resolve_doctl_binary", lambda: Path("/doctl")
    )
    monkeypatch.setattr("catalpa_tooling.deploy_do_link.find_droplet_for_link", fake_find)
    monkeypatch.setattr(
        "catalpa_tooling.dns_resolve.verify_public_dns_from_info", fake_public
    )
    assert cmd_env_host(config, "staging", write=False) == 0
    assert find_called is False
    assert public_calls == ["203.0.113.5"]
    assert "digitalocean.disabled" in capsys.readouterr().err


def test_cmd_env_host_disabled_write_fails(tmp_path: Path) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "staging"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        "docker_host: ssh://root@203.0.113.5\n"
        "digitalocean:\n  disabled: true\n",
        encoding="utf-8",
    )
    assert cmd_env_host(config, "staging", write=True) == 1


def test_cmd_env_host_create_rejects_disabled(tmp_path: Path) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host_create

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "staging"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        "digitalocean:\n  disabled: true\n",
        encoding="utf-8",
    )
    assert cmd_env_host_create(config, "staging", ["--dry-run"]) == 1


def test_cmd_env_host_create_wait_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host_create

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("description: test\n", encoding="utf-8")

    create_kwargs: list[dict] = []
    ensure_calls: list[str] = []

    def fake_create(*_a, **kwargs):
        create_kwargs.append(kwargs)
        return 0

    def fake_ensure(dh: str, **kw: object) -> int:
        ensure_calls.append(dh)
        return 0

    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.ensure_doctl_available", lambda: Path("/doctl")
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.try_resolve_doctl_binary", lambda: Path("/doctl")
    )
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.resolve_project_id",
        lambda *_a, **_k: "proj-1",
    )
    monkeypatch.setattr("catalpa_tooling.doctl_droplets.create_droplet", fake_create)
    monkeypatch.setattr(
        "catalpa_tooling.deploy_do_link.find_droplet_for_link",
        lambda *_a, **_k: _active_droplet("test-prod"),
    )
    monkeypatch.setattr(
        "catalpa_tooling.ssh_known_hosts.ensure_ssh_known_host_for_docker_host",
        fake_ensure,
    )
    monkeypatch.setattr("catalpa_tooling.doctl_domains.sync_host_dns", lambda *_a, **_k: 0)
    dns_check_calls: list[bool] = []

    def fake_dns_checks(*_a, **kwargs):
        dns_check_calls.append(kwargs.get("include_do_api", False))
        return 0

    monkeypatch.setattr("catalpa_tooling.deploy_do_link._run_host_dns_checks", fake_dns_checks)

    assert (
        cmd_env_host_create(
            config,
            "prod",
            ["--size", "s-1vcpu-1gb", "--region", "sgp1"],
        )
        == 0
    )
    assert dns_check_calls == [True]
    assert create_kwargs[0]["wait"] is True
    assert create_kwargs[0]["for_env"] is None
    assert create_kwargs[0]["dry_run"] is False
    assert ensure_calls == ["ssh://root@203.0.113.5"]


def test_cmd_env_host_create_dry_run_skips_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host_create

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("description: test\n", encoding="utf-8")

    write_called = False

    def fake_create(*_a, **kwargs):
        assert kwargs["wait"] is True
        assert kwargs["dry_run"] is True
        return 0

    def fake_host_write(*_a, **_k):
        nonlocal write_called
        write_called = True
        return 0

    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.resolve_project_id_dry_run",
        lambda *_a, **_k: "proj-1",
    )
    monkeypatch.setattr("catalpa_tooling.doctl_droplets.create_droplet", fake_create)
    monkeypatch.setattr("catalpa_tooling.deploy_do_link.cmd_env_host", fake_host_write)

    assert cmd_env_host_create(config, "prod", ["--dry-run"], global_dry_run=False) == 0
    assert write_called is False


def test_cmd_env_host_create_rejects_wait_flag(tmp_path: Path) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host_create

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("description: test\n", encoding="utf-8")
    assert cmd_env_host_create(config, "prod", ["--wait"]) == 1


def test_cmd_env_host_create_uses_info_yaml_size_region(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host_create

    config = _load_test_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "staging"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        "digitalocean:\n  size: s-1vcpu-2gb\n  region: sgp1\n",
        encoding="utf-8",
    )

    create_kwargs: list[dict] = []

    def fake_create(*_a, **kwargs):
        create_kwargs.append(kwargs)
        return 0

    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.resolve_project_id_dry_run",
        lambda *_a, **_k: "proj-1",
    )
    monkeypatch.setattr("catalpa_tooling.doctl_droplets.create_droplet", fake_create)
    monkeypatch.setattr(
        "catalpa_tooling.deploy_do_link.cmd_env_host",
        lambda *_a, **_k: 0,
    )
    monkeypatch.setattr("catalpa_tooling.doctl_domains.sync_host_dns", lambda *_a, **_k: 0)
    monkeypatch.setattr("catalpa_tooling.doctl_domains.verify_host_dns", lambda *_a, **_k: 0)

    assert cmd_env_host_create(config, "staging", ["--dry-run"]) == 0
    assert create_kwargs[0]["env_size"] == "s-1vcpu-2gb"
    assert create_kwargs[0]["env_region"] == "sgp1"
    assert create_kwargs[0]["size"] is None
    assert create_kwargs[0]["region"] is None


def test_cmd_env_host_create_cli_overrides_info_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling.deploy_do_link import cmd_env_host_create

    config = _load_test_config(tmp_path)
    tooling_path = tmp_path / "tooling.yaml"
    data = yaml.safe_load(tooling_path.read_text(encoding="utf-8"))
    data["digitalocean"] = {"size": "s-4vcpu-8gb", "region": "nyc1"}
    tooling_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    from catalpa_tooling.config import load_project_config

    config = load_project_config(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        "digitalocean:\n  size: s-1vcpu-2gb\n  region: sgp1\n",
        encoding="utf-8",
    )

    create_kwargs: list[dict] = []

    def fake_create(*_a, **kwargs):
        create_kwargs.append(kwargs)
        return 0

    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.resolve_project_id_dry_run",
        lambda *_a, **_k: "proj-1",
    )
    monkeypatch.setattr("catalpa_tooling.doctl_droplets.create_droplet", fake_create)
    monkeypatch.setattr(
        "catalpa_tooling.deploy_do_link.cmd_env_host",
        lambda *_a, **_k: 0,
    )

    assert (
        cmd_env_host_create(
            config,
            "prod",
            ["--dry-run", "--size", "s-2vcpu-4gb", "--region", "fra1"],
        )
        == 0
    )
    assert create_kwargs[0]["size"] == "s-2vcpu-4gb"
    assert create_kwargs[0]["region"] == "fra1"
    assert create_kwargs[0]["env_size"] == "s-1vcpu-2gb"
    assert create_kwargs[0]["env_region"] == "sgp1"
