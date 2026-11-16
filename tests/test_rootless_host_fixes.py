"""dk commands that ran as root, or wrote secrets world-readable, on a deploy host."""

import subprocess

import pytest

from catalpa_tooling import docker_logging, pgbackrest_volume_config as pvc

WRITE_ENV = {
    "COMPOSE_PROJECT_NAME": "site",
    "PGBR_S3_WRITE_BUCKET": "b",
    "PGBR_S3_WRITE_REGION": "r",
    "PGBR_S3_WRITE_KEY": "k",
    "PGBR_S3_WRITE_SECRET": "s",
    "PGBR_S3_WRITE_REPO_PATH": "/p",
    "PGBR_S3_WRITE_STANZA": "main",
}


def _record(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(pvc, "run_cmd", fake_run)
    return calls


def test_stanza_create_runs_as_postgres(monkeypatch) -> None:
    """pgBackRest refuses stanza-create as root; the catalpa-site db image sets no USER."""
    calls = _record(monkeypatch)
    monkeypatch.setattr(pvc, "_pgdata_has_control_file", lambda *a, **k: True)
    assert pvc.run_pgbackrest_stanza_create(dict(WRITE_ENV), image="img") == 0
    run = next(c for c in calls if any("stanza-create" in a for a in c))
    assert run[run.index("--user") + 1] == "postgres"
    assert run.index("--user") < run.index("img"), "a docker run option, not a container arg"


def test_config_written_into_volumes_is_not_world_readable(monkeypatch) -> None:
    """The pgBackRest drop-in carries the repository's S3 secret."""
    calls = _record(monkeypatch)
    pvc._docker_run_cp("vol", "50-managed.conf", "secret", {}, image="img")
    script = calls[0][-1]
    assert "chmod 600 /work/50-managed.conf" in script
    assert "644" not in script


def test_host_logging_does_nothing_on_a_rootless_podman_host(monkeypatch, capsys) -> None:
    info = {"docker_host": "ssh://core@vm/run/user/1000/podman/podman.sock"}
    monkeypatch.setattr(
        "catalpa_tooling.deploy_do_link.load_env_info", lambda config, env: ("info.yaml", info)
    )

    def no_ssh(*_a, **_k):
        pytest.fail("host logging reached the host")

    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts.ensure_ssh_known_host_for_docker_host", no_ssh)
    monkeypatch.setattr(docker_logging, "probe_host_logging", no_ssh)
    assert docker_logging.cmd_host_logging(object(), "fcos", apply=True, yes=True) == 0
    assert "rootless Podman" in capsys.readouterr().out


def test_host_logging_still_checks_a_docker_host(monkeypatch) -> None:
    """Only a rootless Podman app host is skipped; a Docker host is probed as before."""
    info = {"docker_host": "ssh://root@prod.example.com"}
    monkeypatch.setattr(
        "catalpa_tooling.deploy_do_link.load_env_info", lambda config, env: ("info.yaml", info)
    )
    monkeypatch.setattr(
        "catalpa_tooling.ssh_known_hosts.ensure_ssh_known_host_for_docker_host", lambda *_a: 0
    )
    probed: list[str] = []
    monkeypatch.setattr(docker_logging, "probe_host_logging", lambda ssh: probed.append(ssh))
    assert docker_logging.cmd_host_logging(object(), "prod") == 1  # probe returned None
    assert probed == ["root@prod.example.com"]


@pytest.mark.parametrize(
    ("env", "initialised", "refused"),
    [
        (WRITE_ENV, False, True),  # new database + backups: archiving would crash migrations
        (WRITE_ENV, True, False),  # already running: never stopped
        ({"COMPOSE_PROJECT_NAME": "site"}, False, False),  # no backups configured
    ],
)
def test_first_up_waits_for_db_init_only_with_backups_and_a_new_database(
    monkeypatch, env, initialised, refused
) -> None:
    monkeypatch.setattr(pvc, "_pgdata_has_control_file", lambda *a, **k: initialised)
    why = pvc.first_up_needs_db_init(dict(env), image="img")
    assert (why is not None) == refused
    if refused:
        assert "db init" in why
