"""Tests for cloud-config template rendering."""

from catalpa_tooling.cloud_config.render import (
    DEFAULT_TIMEZONE,
    bootstrap_template_path,
    render_droplet_bootstrap,
)


def test_bootstrap_template_path_exists() -> None:
    path = bootstrap_template_path()
    assert path.is_file()
    assert path.name == "droplet-bootstrap.yaml"


def test_render_droplet_bootstrap_starts_with_cloud_config() -> None:
    text = render_droplet_bootstrap()
    assert text.startswith("#cloud-config\n")


def test_render_droplet_bootstrap_timezone() -> None:
    text = render_droplet_bootstrap(timezone="Pacific/Auckland")
    assert "timezone: Pacific/Auckland" in text


def test_render_droplet_bootstrap_default_timezone() -> None:
    text = render_droplet_bootstrap()
    assert f"timezone: {DEFAULT_TIMEZONE}" in text


def test_render_droplet_bootstrap_docker_ce() -> None:
    text = render_droplet_bootstrap()
    assert "download.docker.com" in text
    assert "docker-ce" in text
    assert "docker-compose-plugin" in text


def test_render_droplet_bootstrap_ssh_hardening() -> None:
    text = render_droplet_bootstrap()
    assert "PasswordAuthentication no" in text
    assert "ssh_pwauth: false" in text


def test_render_droplet_bootstrap_ufw() -> None:
    text = render_droplet_bootstrap()
    assert "ufw allow OpenSSH" in text
    assert "ufw --force enable" in text


def test_render_droplet_bootstrap_unattended_reboot() -> None:
    text = render_droplet_bootstrap()
    assert 'Unattended-Upgrade::Automatic-Reboot "true"' in text
    assert 'Unattended-Upgrade::Automatic-Reboot-Time "04:00"' in text


def test_render_droplet_bootstrap_unattended_docker_origin() -> None:
    text = render_droplet_bootstrap()
    assert "53catalpa-unattended-docker.conf" in text
    assert "origin=Docker,archive=${distro_codename}" in text
