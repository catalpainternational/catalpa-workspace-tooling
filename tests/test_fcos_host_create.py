"""`dk <env> host create` for a Fedora CoreOS droplet (`digitalocean.os: fedora-coreos`)."""

import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from catalpa_tooling import doctl_droplets
from catalpa_tooling.cloud_config.fcos_ignition import render_fcos_ignition
from catalpa_tooling.deploy_do_link import format_docker_host, resolve_env_do_link

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDnTMNeoxB4M0vwsbC02XXQop9iz8vHD+/L5lNqqmuCO op"


def _files(ign: dict) -> dict[str, str]:
    return {f["path"]: unquote(f["contents"]["source"].removeprefix("data:,")) for f in ign["storage"]["files"]}


def test_ignition_gives_core_its_keys_and_no_passwordless_sudo() -> None:
    ign = json.loads(render_fcos_ignition([KEY]))
    assert ign["passwd"]["users"] == [{"name": "core", "sshAuthorizedKeys": [KEY]}]
    sudoers = _files(ign)["/etc/sudoers.d/coreos-sudo-group"]
    assert [ln for ln in sudoers.splitlines() if ln and not ln.startswith("#")] == []
    assert "/var/lib/systemd/linger/core" in _files(ign)
    links = {link["path"].rsplit("/", 1)[1]: link["target"] for link in ign["storage"]["links"]}
    assert links["podman.socket"] == "/usr/lib/systemd/user/podman.socket"
    assert links["podman-restart.service"] == "/usr/lib/systemd/user/podman-restart.service"


def test_ignition_refuses_a_droplet_nobody_can_log_in_to() -> None:
    with pytest.raises(ValueError, match="SSH public key"):
        render_fcos_ignition([])


def test_fcos_env_logs_in_as_core_and_reaches_its_podman(minimal_project) -> None:
    info = {"digitalocean": {"os": "fedora-coreos", "image": "fcos-next-45"}}
    link = resolve_env_do_link(minimal_project, "fcos", info)
    assert (link.ssh_user, link.os, link.image) == ("core", "fedora-coreos", "fcos-next-45")
    assert format_docker_host(link.ssh_user, "203.0.113.5", os=link.os) == (
        "ssh://core@203.0.113.5/run/user/1000/podman/podman.sock"
    )
    assert format_docker_host("root", "203.0.113.5") == "ssh://root@203.0.113.5"


def test_create_sends_ignition_for_the_fcos_image_without_monitoring(monkeypatch) -> None:
    sent: dict = {}

    def fake_doctl_json(argv, **_kw):
        sent["argv"] = list(argv)
        sent["user_data"] = Path(argv[argv.index("--user-data-file") + 1]).read_text()
        return [{"id": 7, "name": "site-fcos"}]

    monkeypatch.setattr(doctl_droplets, "fcos_image_id", lambda image, **_k: "4242")
    monkeypatch.setattr(doctl_droplets, "ssh_public_keys", lambda refs, **_k: [KEY])
    monkeypatch.setattr(doctl_droplets, "_resolve_existing_droplet_for_create", lambda *a, **k: None)
    monkeypatch.setattr(doctl_droplets, "_ensure_created_droplets_in_project", lambda *a, **k: 0)
    monkeypatch.setattr(doctl_droplets, "_print_created_droplet_summary", lambda d: None)
    monkeypatch.setattr("catalpa_tooling.doctl_binary.ensure_doctl_available", lambda: None)
    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl_json", fake_doctl_json)
    rc = doctl_droplets.create_droplet(
        "site-fcos", size="s-2vcpu-4gb", region="sgp1", project_id="p", ssh_keys=("1",),
        env_image="fcos-next-45", os="fedora-coreos",
    )  # fmt: skip
    assert rc == 0
    argv = sent["argv"]
    assert argv[argv.index("--image") + 1] == "4242"
    assert "--enable-monitoring" not in argv
    assert json.loads(sent["user_data"])["ignition"]["version"].startswith("3.")


def test_ignition_lets_core_bind_80_and_reboots_only_in_the_window() -> None:
    files = _files(json.loads(render_fcos_ignition([KEY])))
    assert files["/etc/sysctl.d/90-dk-unprivileged-ports.conf"].strip() == "net.ipv4.ip_unprivileged_port_start = 80"
    window = files["/etc/zincati/config.d/55-dk-update-window.toml"]
    assert 'strategy = "periodic"' in window and 'days = [ "Sun" ]' in window


def test_the_fcos_image_is_found_by_its_name_and_must_be_unique(monkeypatch) -> None:
    images = [{"id": 11, "name": "fcos-a"}, {"id": 12, "name": "fcos-b"}, {"id": 13, "name": "fcos-b"}]
    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl_json", lambda *_a, **_k: images)
    assert doctl_droplets.fcos_image_id("fcos-a") == "11"
    assert doctl_droplets.fcos_image_id("4242") == "4242"
    with pytest.raises(SystemExit):
        doctl_droplets.fcos_image_id("fcos-b")


def test_core_gets_the_public_keys_named_by_id_or_fingerprint(monkeypatch) -> None:
    keys = [
        {"id": 1, "fingerprint": "aa:bb", "public_key": "ssh-ed25519 AAAA one"},
        {"id": 2, "fingerprint": "cc:dd", "public_key": "ssh-ed25519 BBBB two"},
        {"id": 3, "fingerprint": "ee:ff", "public_key": "ssh-ed25519 CCCC three"},
    ]
    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl_json", lambda *_a, **_k: keys)
    assert doctl_droplets.ssh_public_keys(("1", "cc:dd")) == ["ssh-ed25519 AAAA one", "ssh-ed25519 BBBB two"]


def test_an_fcos_droplet_needs_an_image(monkeypatch, capsys) -> None:
    """There is no stock FCOS slug, so a missing image is refused before any lookup."""
    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl_json", lambda *_a, **_k: pytest.fail("looked up"))
    with pytest.raises(SystemExit):
        doctl_droplets.create_droplet(
            "x", size="s-1vcpu-2gb", region="sgp1", project_id="p", ssh_keys=("1",), os="fedora-coreos",
        )  # fmt: skip
    assert "Missing image" in capsys.readouterr().err
