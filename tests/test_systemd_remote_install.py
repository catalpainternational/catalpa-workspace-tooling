"""Tests for catalpa_tooling.systemd_remote_install."""

import pytest

from catalpa_tooling.systemd_remote_install import (
    backup_install_target,
    rootless_podman_socket,
    parse_docker_host_to_ssh_target,
    parse_install_systemd_flags,
    redact_env_file_content,
    render_pgbackrest_env,
    render_restic_env,
)


def test_parse_docker_host_ssh_url() -> None:
    assert parse_docker_host_to_ssh_target("ssh://root@staging.example.com") == "root@staging.example.com"


def test_parse_docker_host_user_at_host() -> None:
    assert parse_docker_host_to_ssh_target("deploy@10.0.0.5") == "deploy@10.0.0.5"


def test_parse_docker_host_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_docker_host_to_ssh_target("")


def test_parse_docker_host_no_user() -> None:
    with pytest.raises(ValueError, match="no SSH user"):
        parse_docker_host_to_ssh_target("ssh://hostonly.example")


def test_parse_install_flags() -> None:
    assert parse_install_systemd_flags([]) == (False, False, None)
    assert parse_install_systemd_flags(["--dry-run", "--enable"]) == (True, True, None)
    assert parse_install_systemd_flags(["--only", "restic"]) == (False, False, "restic")


def test_parse_install_flags_fixed_only() -> None:
    assert parse_install_systemd_flags([], fixed_only="pgbackrest") == (False, False, "pgbackrest")
    assert parse_install_systemd_flags(["--dry-run"], fixed_only="restic") == (True, False, "restic")


def test_parse_install_flags_rejects_only_with_fixed() -> None:
    with pytest.raises(ValueError, match="not valid"):
        parse_install_systemd_flags(["--only", "restic"], fixed_only="pgbackrest")


def test_render_pgbackrest_env_uses_project_name() -> None:
    body = render_pgbackrest_env(
        {"PGBR_DB_CONTAINER": "db1", "PGBR_STANZA": "main"},
        project_name="marktwain",
    )
    assert "# Managed by marktwain deploy" in body


def test_render_restic_env_includes_aws_from_s3_keys() -> None:
    body = render_restic_env(
        {
            "RESTIC_REPOSITORY": "s3:s3.amazonaws.com/bucket",
            "RESTIC_PASSWORD": "pw",
            "RESTIC_S3_ACCESS_KEY_ID": "AKIA",
            "RESTIC_S3_SECRET_ACCESS_KEY": "secret",
            "RESTIC_S3_DEFAULT_REGION": "us-east-1",
        }
    )
    assert "AWS_ACCESS_KEY_ID=AKIA" in body
    assert "AWS_SECRET_ACCESS_KEY=secret" in body
    assert "AWS_DEFAULT_REGION=us-east-1" in body


def test_redact_env_file_content() -> None:
    raw = "RESTIC_REPOSITORY=s3:bucket/x\nRESTIC_PASSWORD=secret\nCOMPOSE_PROJECT_NAME=pas_indmo\n"
    out = redact_env_file_content(raw)
    assert "secret" not in out
    assert "<redacted>" in out
    assert "pas_indmo" in out


def test_backup_install_target_docker_host_is_root_systemd() -> None:
    t = backup_install_target(
        "ssh://root@prod.example.com", install_prefix="/opt/site", config_dir="/etc/site"
    )
    assert (t.unit_dir, t.install_prefix, t.config_dir) == ("/etc/systemd/system", "/opt/site", "/etc/site")
    assert (t.systemctl, t.docker, t.user) == ("systemctl", "docker", False)


def test_backup_install_target_rootless_podman_is_user_systemd() -> None:
    """A rootless host (no sudo) can't write /opt, /etc or /etc/systemd/system."""
    t = backup_install_target(
        "ssh://core@vm/run/user/1000/podman/podman.sock",
        install_prefix="/opt/catalpa-site",
        config_dir="/etc/catalpa-site",
    )
    assert t.user is True
    assert t.unit_dir == ".config/systemd/user"
    assert (t.install_prefix, t.config_dir) == (".local/lib/catalpa-site", ".config/catalpa-site")
    assert (t.unit_install_prefix, t.unit_config_dir) == (
        "%h/.local/lib/catalpa-site",
        "%h/.config/catalpa-site",
    )
    assert t.systemctl == "systemctl --user"
    assert t.docker == "DOCKER_HOST=unix:///run/user/1000/podman/podman.sock docker"


def _record_remote(monkeypatch):
    """Record every ssh/scp the installer runs, with the content of each file it copies."""
    import subprocess
    from pathlib import Path

    from catalpa_tooling import systemd_remote_install as sri

    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd, **_kw):
        copied = {Path(a).name: Path(a).read_text() for a in cmd[:-1] if Path(a).is_file()}
        calls.append((list(cmd), copied))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(sri, "run_cmd", fake_run)
    return calls


def test_install_on_rootless_podman_host_uses_user_systemd(minimal_project, monkeypatch) -> None:
    from catalpa_tooling.systemd_remote_install import cmd_install_systemd_backups

    calls = _record_remote(monkeypatch)
    rc = cmd_install_systemd_backups(
        minimal_project,
        {"PGBR_S3_WRITE_STANZA": "main", "PGBR_DB_CONTAINER": "site-db-1"},
        "ssh://core@vm/run/user/1000/podman/podman.sock",
        "fcos",
        ["--enable"],
        global_dry_run=False,
        yes=True,
        fixed_only="pgbackrest",
    )
    assert rc == 0
    remote = [c[-1] for c, _ in calls if c[0] == "ssh"]
    assert not [r for r in remote if "sudo" in r or "/etc/" in r or "/opt/" in r]
    assert "systemctl --user daemon-reload" in remote
    assert any(r.startswith("systemctl --user enable --now ") for r in remote)
    unit_copy = next(c for c, _ in calls if c[0] == "scp" and c[-1].endswith(":.config/systemd/user"))
    services = {
        name: body for _, files in calls for name, body in files.items() if name.endswith(".service")
    }
    assert unit_copy and services
    for body in services.values():
        assert "Environment=DOCKER_HOST=unix://%t/podman/podman.sock" in body
        assert "User=root" not in body


def test_install_on_docker_host_is_root_systemd(minimal_project, monkeypatch) -> None:
    from catalpa_tooling.systemd_remote_install import cmd_install_systemd_backups

    calls = _record_remote(monkeypatch)
    rc = cmd_install_systemd_backups(
        minimal_project,
        {"PGBR_S3_WRITE_STANZA": "main", "PGBR_DB_CONTAINER": "site-db-1"},
        "ssh://root@prod.example.com",
        "prod",
        ["--enable"],
        global_dry_run=False,
        yes=True,
        fixed_only="pgbackrest",
    )
    assert rc == 0
    remote = [c[-1] for c, _ in calls if c[0] == "ssh"]
    assert "systemctl daemon-reload" in remote
    assert any("/etc/systemd/system" in c[-1] for c, _ in calls if c[0] == "scp")
    services = [b for _, f in calls for n, b in f.items() if n.endswith(".service")]
    assert services and all("User=root" in b for b in services)


@pytest.mark.parametrize(
    "docker_host",
    [
        "ssh://root@prod.example.com",
        "ssh://u@h/run/user/1000/docker.sock",  # rootless Docker, not Podman
        "ssh://u@h/var/run/podman/podman.sock",  # rootful Podman
    ],
)
def test_rootless_podman_socket_only_for_a_users_podman(docker_host: str) -> None:
    assert rootless_podman_socket(docker_host) is None


def test_rootless_podman_socket_finds_the_users_podman() -> None:
    host = "ssh://core@vm/run/user/1000/podman/podman.sock"
    assert rootless_podman_socket(host) == "/run/user/1000/podman/podman.sock"
