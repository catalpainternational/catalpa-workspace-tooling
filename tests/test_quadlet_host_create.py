"""Tooling 2.0 M6: ``dk <env> host create`` for a rendered host, and ``storage.data_volume``
shared by both runtimes. doctl, the render check, Butane and provisioning are faked."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.deploy_do_link import cmd_env_host_create
from catalpa_tooling.fcos.settings import FcosConfigError, parse_fcos_settings
from catalpa_tooling.storage_config import (
    StorageConfigError,
    parse_data_volume,
    parse_storage_volumes_from_info,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"
IP = "203.0.113.20"
DO = "digitalocean:\n  droplet_name: site-staging\n  image: '123456'\n  region: sgp1\n  size: s-2vcpu-4gb\n"


# --- storage.data_volume ---------------------------------------------------------------------


def test_data_volume_absent_is_the_quadlet_default() -> None:
    s = parse_fcos_settings({"fcos": {"hostname": "fcos"}})
    assert (s.data_mount, s.block_volume, s.block_volume_size, s.block_volume_filesystem) == (
        "/var/mnt/data", None, 10, "xfs",
    )  # fmt: skip


def test_data_volume_settings() -> None:
    info = {"storage": {"data_volume": {"path": "/srv/data", "digitalocean": {"name": "x-data", "size_gib": 50}}}}
    s = parse_fcos_settings(info)
    assert (s.data_mount, s.block_volume, s.block_volume_size) == ("/srv/data", "x-data", 50)
    s = parse_fcos_settings({"storage": {"data_volume": {"path": "/srv/data"}}})
    assert (s.data_mount, s.block_volume) == ("/srv/data", False)  # no DO volume: the root disk


@pytest.mark.parametrize(
    "info,message",
    [
        ({"storage": {"data_volume": {"digitalocean": {"name": "x"}}}}, "size_gib is required"),
        ({"storage": {"data_volume": {"path": "relative"}}}, "absolute path"),
        ({"storage": {"data_volume": {"pth": "/x"}}}, "unknown keys pth"),
        ({"storage": {"volumes": {"pgdata": {"path": "/x"}}}}, "storage.data_volume"),
        ({"fcos": {"data_mount": "/x"}}, "unknown fcos keys: data_mount"),
        ({"fcos": {"block_volume": "x"}}, "unknown fcos keys: block_volume"),
    ],
)
def test_bad_or_old_storage_settings_are_refused(info, message) -> None:
    with pytest.raises(FcosConfigError, match=message):
        parse_fcos_settings(info)


def test_compose_envs_dont_take_data_volume_yet(minimal_project) -> None:
    info = {"storage": {"data_volume": {"path": "/mnt/data"}}}
    with pytest.raises(StorageConfigError, match="M7"):
        parse_storage_volumes_from_info(info, minimal_project)
    # A quadlet env's storage.volumes aren't compose's to read.
    assert parse_storage_volumes_from_info({"runtime": "quadlet", **info}, minimal_project) == {}
    assert parse_data_volume({}) is None


# --- host create ------------------------------------------------------------------------------


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "site"
    shutil.copytree(FIXTURES / "site", root, ignore=shutil.ignore_patterns("expected"))
    env = root / "docker" / "envs" / "staging"
    shutil.copytree(FIXTURES / "site" / "expected", env / "generated" / "fcos")
    info = env / "info.yaml"
    info.write_text(info.read_text() + DO)
    monkeypatch.chdir(root)
    return load_project_config(root), info


@pytest.fixture
def do(monkeypatch: pytest.MonkeyPatch):
    """Records what would change on DigitalOcean and the host."""
    state = {"droplet": None, "created": False, "volume": None, "calls": []}
    calls = state["calls"]

    monkeypatch.setattr("catalpa_tooling.fcos.render.render_env", lambda config, env: object())
    monkeypatch.setattr("catalpa_tooling.fcos.cli._check", lambda *a, **k: 0)
    monkeypatch.setattr("catalpa_tooling.fcos.host.compile_ignition", lambda gen_dir: b'{"ignition":{}}')
    monkeypatch.setattr("catalpa_tooling.doctl_binary.ensure_doctl_available", lambda: None)
    monkeypatch.setattr("catalpa_tooling.doctl_projects.resolve_project_id", lambda *a, **k: "proj")
    monkeypatch.setattr("catalpa_tooling.doctl_projects.resolve_project_id_dry_run", lambda *a, **k: "proj")

    def find_droplet(config, link, **k):
        if state["created"]:
            return {"id": 7, "name": link.droplet_name, "networks": {"v4": [{"type": "public", "ip_address": IP}]}}
        return state["droplet"]

    monkeypatch.setattr("catalpa_tooling.deploy_do_link.find_droplet_for_link", find_droplet)
    monkeypatch.setattr("catalpa_tooling.deploy_do_link._orphan_droplet_message", lambda *a, **k: None)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_block_storage.find_volume_by_name",
        lambda name, **k: state["volume"] if state["volume"] and state["volume"]["name"] == name else None,
    )

    def create_volume(name, size, region, **k):
        calls.append(("create_volume", name, size, region))
        return {"id": "vol-new", "name": name}

    monkeypatch.setattr("catalpa_tooling.doctl_block_storage.create_volume", create_volume)
    monkeypatch.setattr(
        "catalpa_tooling.fcos.host_create._assign_volume_to_project",
        lambda vid, pid, **k: calls.append(("assign", vid, pid)) or 0,
    )

    def create_droplet(name, **k):
        calls.append(("create_droplet", name, k))
        if not k["dry_run"]:
            state["created"] = True
        return 0

    monkeypatch.setattr("catalpa_tooling.doctl_droplets.create_droplet", create_droplet)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_domains.sync_host_dns",
        lambda config, info, droplet_ip, **k: calls.append(("dns", droplet_ip)) or 0,
    )
    monkeypatch.setattr(
        "catalpa_tooling.fcos.cli._provision",
        lambda ns, config, env, c, target, **k: calls.append(("provision", target.address, ns.no_start)) or 0,
    )
    return state


def names(calls) -> list[str]:
    return [c[0] for c in calls]


def test_a_new_host_gets_a_new_volume_the_ignition_dns_and_provisioning(site, do) -> None:
    config, _info = site
    assert cmd_env_host_create(config, "staging", []) == 0
    calls = do["calls"]
    assert names(calls) == ["create_volume", "assign", "create_droplet", "dns", "provision"]
    assert calls[0][1:] == ("site-staging-data", 10, "sgp1")
    _n, droplet, k = calls[2]
    assert droplet == "site-staging"
    assert k["user_data"] == '{"ignition":{}}' and k["volume_ids"] == ("vol-new",)
    assert k["image"] == "123456" and k["enable_monitoring"] is False and k["reuse_existing"] is False
    assert calls[4] == ("provision", IP, False)


def test_a_kept_volume_is_reused_and_no_start_is_passed_on(site, do, capsys) -> None:
    config, _ = site
    do["volume"] = {"id": "vol-kept", "name": "site-staging-data", "size_gigabytes": 10,
                    "droplet_ids": [], "region": {"slug": "sgp1"}}  # fmt: skip
    assert cmd_env_host_create(config, "staging", ["--no-start"]) == 0
    calls = do["calls"]
    assert "create_volume" not in names(calls)
    assert calls[0][2]["volume_ids"] == ("vol-kept",)
    assert calls[-1] == ("provision", IP, True)
    assert "reusing it, with its data" in capsys.readouterr().err


@pytest.mark.parametrize(
    "setup,message",
    [
        (lambda d: d.update(droplet={"id": 1, "networks": {}}), "already exists"),
        (lambda d: d.update(volume={"id": "v", "name": "site-staging-data", "droplet_ids": [99]}), "attached to droplet 99"),
        (lambda d: d.update(volume={"id": "v", "name": "site-staging-data", "droplet_ids": [],
                                    "region": {"slug": "nyc3"}}), "in nyc3"),
    ],
)  # fmt: skip
def test_refusals_change_nothing(site, do, capsys, setup, message) -> None:
    config, _ = site
    setup(do)
    assert cmd_env_host_create(config, "staging", []) == 1
    assert do["calls"] == []
    assert message in capsys.readouterr().err


def test_no_image_is_refused(site, do, capsys) -> None:
    config, info = site
    info.write_text(info.read_text().replace("  image: '123456'\n", ""))
    assert cmd_env_host_create(config, "staging", []) == 1
    assert "digitalocean.image" in capsys.readouterr().err and do["calls"] == []


def test_dry_run_creates_nothing(site, do, capsys) -> None:
    config, info = site
    before = info.read_text()
    assert cmd_env_host_create(config, "staging", ["--dry-run"]) == 0
    assert names(do["calls"]) == ["create_droplet"] and do["calls"][0][2]["dry_run"] is True
    assert info.read_text() == before
    assert "would create volume site-staging-data (10 GiB, sgp1)" in capsys.readouterr().err


def test_no_provision_stops_after_dns(site, do) -> None:
    config, _ = site
    assert cmd_env_host_create(config, "staging", ["--", "--no-provision"]) == 0
    assert names(do["calls"])[-1] == "dns"


def test_docker_host_by_ip_follows_the_new_droplet(site, do) -> None:
    config, info = site
    text = info.read_text().replace("deploy@staging.site.example", "deploy@198.51.100.4")
    info.write_text(text)
    assert cmd_env_host_create(config, "staging", []) == 0
    assert f"docker_host: ssh://deploy@{IP}/run/user/1001/podman/podman.sock" in info.read_text()
    # By hostname, it stays (test above: the fixture's docker_host names staging.site.example).


def test_a_compose_env_still_gets_cloud_config(minimal_project, monkeypatch) -> None:
    env = minimal_project.deploy_envs_dir / "prod"
    env.mkdir(parents=True, exist_ok=True)
    (env / "info.yaml").write_text("name: prod\n")
    seen = []
    monkeypatch.setattr(
        "catalpa_tooling.fcos.host_create.cmd_quadlet_host_create", lambda *a, **k: seen.append(a) or 0
    )
    monkeypatch.setattr("catalpa_tooling.doctl_droplets.create_droplet", lambda *a, **k: seen.append("compose") or 0)
    monkeypatch.setattr("catalpa_tooling.doctl_projects.resolve_project_id_dry_run", lambda *a, **k: "proj")
    cmd_env_host_create(minimal_project, "prod", ["--dry-run"])
    assert seen == ["compose"]


def test_project_and_context_apply_to_every_step(site, do, monkeypatch) -> None:
    """As for compose (Josh's 1e6c12b): the droplet is looked for where it was made."""
    config, _ = site
    seen = []
    import catalpa_tooling.deploy_do_link as ddl

    orig = ddl.find_droplet_for_link
    monkeypatch.setattr(
        ddl, "find_droplet_for_link",
        lambda config, link, **k: seen.append((config.digitalocean.project_name, config.digitalocean.context))
        or orig(config, link, **k),
    )  # fmt: skip
    assert cmd_env_host_create(config, "staging", ["--", "--project", "Other", "--context", "work"]) == 0
    assert seen and set(seen) == {("Other", "work")}
