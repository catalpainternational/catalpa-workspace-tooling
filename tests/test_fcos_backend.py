"""dk's stack commands on a ``runtime: quadlet`` env (``fcos/backend.py``), against the site
fixture's golden render, with SSH faked."""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.fcos import backend

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"


@pytest.fixture(autouse=True)
def credentials_in_place(monkeypatch):
    """Backup credentials exist (their provisioning is tested in test_fcos_backup_credentials)."""
    from catalpa_tooling.fcos import backup_credentials

    monkeypatch.setattr(backup_credentials, "ensure_backup_credentials", lambda *a, **k: 0)


@pytest.fixture
def site(tmp_path: Path):
    root = tmp_path / "site"
    shutil.copytree(FIXTURES / "site", root)
    shutil.copytree(root / "expected", root / "docker" / "envs" / "staging" / "generated" / "fcos")
    return load_project_config(root)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, input=None, tty=False):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    @property
    def remote(self) -> list[str]:
        """The last call's remote command, split."""
        return shlex.split(self.calls[-1][-1])

    @property
    def ssh_flags(self) -> list[str]:
        return self.calls[-1][:-2]


def run(config, *argv: str, tty: bool = False, **ns_extra) -> tuple[int, Recorder]:
    r = Recorder()
    if argv and argv[0] == "manage":
        ns = argparse.Namespace(env_command="manage", manage_args=list(argv[1:]), dry_run=False, **ns_extra)
    else:
        ns = argparse.Namespace(env_command=None, implicit_compose_argv=list(argv), dry_run=False, **ns_extra)
    info = {"docker_host": "ssh://deploy@staging.site.example/run/user/1001/podman/podman.sock"}
    return backend.handle(ns, config, "staging", info, runner=r, tty=tty), r


def test_logs_follow_selected_services(site) -> None:
    rc, r = run(site, "logs", "-f", "--tail", "50", "django", "db", tty=True)
    assert rc == 0
    assert r.calls[-1][-2] == "deploy@staging.site.example"
    assert "-t" in r.ssh_flags
    assert r.remote == [
        "journalctl", "--user", "--no-pager", "-u", "django.service", "-u", "db.service",
        "-n", "50", "-f",
    ]  # fmt: skip


def test_logs_default_all_services_no_tty_when_not_following(site) -> None:
    rc, r = run(site, "logs", tty=True)
    assert rc == 0
    assert "-t" not in r.ssh_flags
    assert [a for a in r.remote if a.endswith(".service")] == [
        "caddy.service", "collectstatic.service", "db.service", "django.service", "migrate.service",
    ]  # fmt: skip


def test_exec_and_manage(site) -> None:
    rc, r = run(site, "exec", "-T", "-u", "postgres", "db", "psql", "-c", "select 1")
    assert rc == 0
    assert r.remote == ["podman", "exec", "-i", "-u", "postgres", "db", "psql", "-c", "select 1"]
    rc, r = run(site, "exec", "django", "sh", tty=True)
    assert r.remote == ["podman", "exec", "-i", "-t", "django", "sh"]
    assert "-t" in r.ssh_flags
    rc, r = run(site, "manage", "migrate", "--plan", user=None)
    assert rc == 0
    assert r.remote[:3] == ["podman", "exec", "-i"]
    assert r.remote[-4:] == ["django", "./manage.py", "migrate", "--plan"]
    rc, r = run(site, "manage", "shell", user="root")
    assert ["-u", "root"] == r.remote[3:5]


def test_ps(site) -> None:
    rc, r = run(site, "ps")
    assert rc == 0
    cmd = r.calls[-1][-1]
    assert cmd.startswith("systemctl --user --no-pager --legend=false list-units --all ")
    assert "caddy.socket" in cmd and "migrate.service" in cmd and "podman ps -a" in cmd


def test_unit_verbs(site) -> None:
    rc, r = run(site, "restart", "django")
    assert (rc, r.calls[-1][-1]) == (0, "systemctl --user restart django.service")
    rc, r = run(site, "stop")
    # The socket first, or the next request starts the edge again.
    assert r.calls[-1][-1] == (
        "systemctl --user stop caddy.socket caddy.service db.service django.service"
    )
    rc, r = run(site, "start")
    assert r.calls[-1][-1].endswith("django.service caddy.socket")
    rc, r = run(site, "restart")
    assert r.calls[-1][-1] == "systemctl --user restart caddy.service db.service django.service"


@pytest.mark.parametrize(
    "argv",
    [["up", "-d"], ["down"], ["pull"], ["build"], ["run", "django", "sh"], ["config"]],
)
def test_compose_only_verbs_are_refused_without_ssh(site, argv, capsys) -> None:
    rc, r = run(site, *argv)
    assert rc == 2 and r.calls == []
    assert "Available: logs, ps, exec, manage" in capsys.readouterr().err


def test_unknown_service_and_bad_usage(site, capsys) -> None:
    rc, r = run(site, "logs", "nope")
    assert rc == 2 and r.calls == []
    assert "not a service of staging: nope" in capsys.readouterr().err
    rc, r = run(site, "exec", "--privileged", "django", "sh")
    assert rc == 2 and r.calls == []
    rc, r = run(site, "exec", "django")
    assert rc == 2 and r.calls == []
    rc, r = run(site, "restart", "--timeout", "5")
    assert rc == 2 and r.calls == []


def test_without_a_committed_render(site, capsys) -> None:
    shutil.rmtree(site.deploy_envs_dir / "staging" / "generated")
    rc, r = run(site, "ps")
    assert rc == 1 and r.calls == []
    assert "dk <env> render" in capsys.readouterr().err


def test_dk_routes_a_quadlet_env_here(site) -> None:
    from catalpa_tooling.env_handlers import handle_env_command

    ns = argparse.Namespace(
        env_name="staging", env_command=None, implicit_compose_argv=["up"], dry_run=False, tag=None
    )
    assert handle_env_command(ns, site) == 2


# --- db / files (swlt has pgBackRest and restic) ----------------------------------------------


@pytest.fixture
def swlt(tmp_path: Path):
    root = tmp_path / "swlt"
    shutil.copytree(FIXTURES / "swlt", root)
    shutil.copytree(root / "expected", root / "docker" / "envs" / "staging" / "generated" / "fcos")
    return load_project_config(root)


def run_sub(config, command: str, sub: str, **extra) -> tuple[int, Recorder]:
    r = Recorder()
    ns = argparse.Namespace(env_command=command, **{"dry_run": False, f"{command}_command": sub, **extra})
    info = {"docker_host": "ssh://deploy@staging.site.example/run/user/1001/podman/podman.sock"}
    return backend.handle(ns, config, "staging", info, runner=r, tty=False), r


def test_db_commands(swlt) -> None:
    rc, r = run_sub(swlt, "db", "info")
    assert rc == 0 and r.remote == [
        "podman", "exec", "-u", "postgres", "db", "pgbackrest", "--log-level-console=info", "info",
    ]  # fmt: skip
    rc, r = run_sub(swlt, "db", "version")
    assert r.remote[-1] == "version" and "--log-level-console=info" not in r.remote
    rc, r = run_sub(swlt, "db", "configure", configure_mode="stanza-create")
    assert r.remote[-1] == "stanza-create"
    rc, r = run_sub(swlt, "db", "backup", backup_type="full")
    assert "systemctl --user start swlt-pgbackrest-backup@full.service" in r.calls[-1][-1]
    assert "journalctl --user -u swlt-pgbackrest-backup@full.service" in r.calls[-1][-1]
    rc, r = run_sub(swlt, "db", "pgdump", pg_dump_args=["--", "-t", "x"])
    assert r.remote[:6] == ["podman", "exec", "-i", "-u", "postgres", "db"]
    assert r.remote[-1].endswith("-Fc -t x")
    rc, r = run_sub(swlt, "bkp_db", "info")  # the old name works too
    assert rc == 0


def test_files_commands(swlt) -> None:
    rc, r = run_sub(swlt, "files", "backup")
    assert "systemctl --user start swlt-restic-files-backup.service" in r.calls[-1][-1]
    rc, r = run_sub(swlt, "files", "snapshots")
    cmd = r.remote
    assert cmd[:4] == ["podman", "run", "--rm", "-i"] and cmd[-1] == "snapshots"
    # The one-off container is the backup unit's: image, ID range, secrets, mounts.
    assert cmd[-2].startswith("docker.io/restic/restic:")
    assert "--uidmap" in cmd and "--secret" in cmd and "-v" in cmd
    assert "restic_write_password,type=env,target=RESTIC_PASSWORD" in cmd
    assert not any(a.startswith("backup") for a in cmd)  # not the unit's Exec=


@pytest.mark.parametrize(
    ("command", "sub", "hint"),
    [
        ("db", "init", "stanza-create"),
        ("db", "install-systemd", "dk staging apply"),
        ("files", "push", "media directory"),
    ],
)
def test_render_owned_backup_commands_are_refused(swlt, command, sub, hint, capsys) -> None:
    rc, r = run_sub(swlt, command, sub)
    assert rc == 2 and r.calls == []
    assert hint in capsys.readouterr().err


def test_files_without_restic(site, capsys) -> None:
    rc, r = run_sub(site, "files", "snapshots")
    assert rc == 2 and r.calls == []
    assert "no restic backup" in capsys.readouterr().err


def test_podman_run_args_reads_only_the_container_section() -> None:
    q = (
        "[Unit]\nDescription=x\n\n[Container]\nContainerName=r\nImage=docker.io/restic/restic:1\n"
        "UIDMap=0:20000:5000\nSecret=a,type=env,target=A\nEnvironment=K=V\n"
        "Volume=/d:/d:ro,z\nExec=backup /d\n# Image=nope\n\n[Service]\nEnvironment=NOT=this\n"
    )
    assert backend.podman_run_args(q) == [
        "--uidmap", "0:20000:5000", "--secret", "a,type=env,target=A", "-e", "K=V",
        "-v", "/d:/d:ro,z", "docker.io/restic/restic:1",
    ]  # fmt: skip


def test_db_restore_plan(swlt, capsys) -> None:
    rc, r = run_sub(swlt, "db", "restore", pgbackrest_restore_args=["--", "--type=time", "--target=2026-11-19 10:00"], yes=False, dry_run=True)
    assert rc == 0 and r.calls == []
    script = capsys.readouterr().err.split(":\n", 1)[1].splitlines()
    assert script[0] == "set -e"
    assert script[1] == "systemctl --user stop django.service db.service"
    run_ = shlex.split(script[2])
    assert run_[:7] == ["podman", "run", "--rm", "--user", "postgres", "--entrypoint", "sh"]
    # db's own settings, secrets and mounts, with quadlet volume units resolved to their names.
    assert "--env-file" in run_ and "--uidmap" in run_
    assert "/var/mnt/data/postgres_data:/var/lib/postgresql:Z" in run_
    assert run_[-1].endswith("restore --delta --type=time '--target=2026-11-19 10:00'")
    assert script[4] == "systemctl --user start django.service"


def test_db_restore_asks_first(swlt, monkeypatch) -> None:
    monkeypatch.setattr("catalpa_tooling.cli_confirm.confirm_by_typing_env_name", lambda env: False)
    rc, r = run_sub(swlt, "db", "restore", pgbackrest_restore_args=[], yes=False)
    assert rc == 1 and r.calls == []
    rc, r = run_sub(swlt, "db", "restore", pgbackrest_restore_args=[], yes=True)
    assert rc == 0 and "pgbackrest --log-level-console=info restore --delta" in r.calls[-1][-1]


def test_db_restore_file_dispatches(swlt, tmp_path, monkeypatch) -> None:
    from catalpa_tooling.fcos import dbdump

    seen = []
    monkeypatch.setattr(dbdump, "restore_archive", lambda b, path, *a, **k: seen.append(path) or 0)
    dump = tmp_path / "x.dump"
    rc, r = run_sub(swlt, "db", "restore", archive_file=str(dump), yes=True)
    assert rc == 0 and seen == [dump]
    rc, r = run_sub(swlt, "db", "restore", archive_file=str(dump), yes=False, dry_run=True)
    assert rc == 0 and seen == [dump]  # dry-run: not called again


def test_files_restore(swlt) -> None:
    rc, r = run_sub(swlt, "files", "restore", snapshot="latest", yes=True)
    assert rc == 0
    lines = r.calls[-1][-1].splitlines()
    cmd = shlex.split(lines[1])
    assert cmd[:3] == ["podman", "run", "--rm"]
    i = cmd.index("restore")
    assert cmd[i:] == [
        "restore", "latest:/backup/django_media", "--target", "/backup/django_media",
        "--delete", "--host", "swlt-shape-staging",
    ]  # fmt: skip
    # The media mount is writable for the restore (the backup unit mounts it read-only).
    media = [a for a in cmd if a.endswith(":/backup/django_media") or ":/backup/django_media:" in a]
    assert media and not any(",ro" in m or ":ro" in m for m in media)
    rc, r = run_sub(swlt, "files", "restore", snapshot="abc123", yes=True)
    assert "--host" not in r.calls[-1][-1]


def test_read_write_drops_only_ro() -> None:
    q = "[Container]\nImage=i\nVolume=/a:/b:ro,z\nVolume=/c:/d:ro\nVolume=/e:/f\n"
    assert backend.podman_run_args(q, read_write=True) == ["-v", "/a:/b:z", "-v", "/c:/d", "-v", "/e:/f", "i"]


def test_volume_units_resolve_to_their_podman_names(site) -> None:
    from catalpa_tooling.fcos.host import load_committed
    from catalpa_tooling.fcos.render import generated_dir

    names = backend.volume_names(load_committed(generated_dir(site, "staging")))
    assert names["caddy_config.volume"] == "site_caddy_config"
    q = "[Container]\nImage=i\nVolume=caddy_config.volume:/config:z\n"
    assert backend.podman_run_args(q, volumes=names) == ["-v", "site_caddy_config:/config:z", "i"]
    with pytest.raises(backend.Usage):
        backend.podman_run_args("[Container]\nImage=i\nVolume=nope.volume:/x\n", volumes=names)


# --- dump restore, media, transfer ----------------------------------------------------------


def _backend(config, runner):
    info = {"docker_host": "ssh://deploy@staging.site.example/run/user/1001/podman/podman.sock"}
    return backend.open_backend(config, "staging", info, runner=runner, register_key=False)


def test_restore_archive_sequence(swlt, tmp_path) -> None:
    import hashlib

    from catalpa_tooling.fcos.dbdump import restore_archive

    dump = tmp_path / "app.dump"
    dump.write_bytes(b"PGDMP" + b"x" * 1000)
    r = Recorder()
    uploads = []
    b = _backend(swlt, r)
    rc = restore_archive(b, dump, uploader=lambda argv, path: uploads.append((argv, path)) or 0)
    assert rc == 0
    cmds = [c[-1] for c in r.calls]
    assert cmds[0].startswith("systemctl --user stop ") and "db.service" not in cmds[0]
    assert "django.service" in cmds[0]
    if b._proxy_socket():  # the edge's socket stops first and starts last
        assert cmds[0].split()[3] == b._proxy_socket()[0]
        assert cmds[5].endswith(b._proxy_socket()[0])
    (up_argv, up_path), = uploads
    assert up_path == dump and "podman exec -i -u root db sh -c" in up_argv[-1]
    remote = up_argv[-1].split("cat > ")[1].split("'")[0]
    assert hashlib.sha256(dump.read_bytes()).hexdigest() in cmds[1] and remote in cmds[1]
    assert "dropdb" in cmds[2] and "createdb" in cmds[2]
    assert "pg_restore" in cmds[3] and remote in cmds[3]
    assert cmds[4] == f"podman exec -u root db rm -f {remote}"
    assert cmds[5].startswith("systemctl --user start ") and "django.service" in cmds[5]


def test_restore_archive_refuses_a_non_archive_and_cleans_up_on_failure(swlt, tmp_path) -> None:
    from catalpa_tooling.fcos.dbdump import restore_archive

    bad = tmp_path / "bad.dump"
    bad.write_bytes(b"not a dump")
    r = Recorder()
    assert restore_archive(_backend(swlt, r), bad) == 1 and r.calls == []

    good = tmp_path / "app.dump"
    good.write_bytes(b"PGDMP" + b"x" * 10)

    class FailRestore(Recorder):
        def __call__(self, argv, *, input=None, tty=False):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 1 if "pg_restore" in argv[-1] else 0, b"", b"")

    r = FailRestore()
    assert restore_archive(_backend(swlt, r), good, uploader=lambda a, p: 0) == 1
    tail = [c[-1] for c in r.calls[-2:]]
    assert tail[0].startswith("podman exec -u root db rm -f ")  # archive removed
    assert tail[1].startswith("systemctl --user start ")  # the app comes back


def test_media_owner_and_push(swlt, tmp_path, monkeypatch) -> None:
    from catalpa_tooling.fcos.settings import SUBUID_START

    r = Recorder()
    b = _backend(swlt, r)
    vol = b.committed.manifest["bind_volumes"]["django_media"]
    # podman unshare: ns uid N is host uid SUBUID_START + N - 1.
    uid = b.media_owner_ns_uid()
    assert SUBUID_START + uid - 1 == vol["owner_uid"]
    cmds = []
    monkeypatch.setattr(
        "catalpa_tooling.run_cmd.run",
        lambda cmd, **k: cmds.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    assert b.push_media(tmp_path) == 0
    cmd = cmds[0]
    assert cmd[0] == "rsync" and "--delete" in cmd
    # No --chown (macOS's openrsync lacks it) and no owner/group from the Mac.
    assert not any(a.startswith("--chown") for a in cmd) and cmd[1] == "-rptDzv"
    assert "--rsync-path=podman unshare rsync" in cmd
    assert cmd[-1] == f"deploy@staging.site.example:{vol['path']}/"
    assert r.calls[-1][-1] == f"podman unshare chown -R {uid}:{uid} {vol['path'].rstrip('/')}"

    # rsync failed or dry run: no chown.
    r.calls.clear()
    assert b.push_media(tmp_path, dry_run=True) == 0 and r.calls == []


def test_pull_media_dispatch(swlt, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        "catalpa_tooling.media_rsync.rsync_pull_remote_to_local", lambda *a: calls.append(a) or 0
    )
    r = Recorder()
    ns = argparse.Namespace(env_command="pull_media", target="/tmp/m", dry_run=False)
    info = {"docker_host": "ssh://deploy@staging.site.example/run/user/1001/podman/podman.sock"}
    assert backend.handle(ns, swlt, "staging", info, runner=r, tty=False) == 0
    assert calls == [("deploy@staging.site.example", "/var/mnt/data/django_media/", Path("/tmp/m").resolve())]


def test_quadlet_to_quadlet_transfer_order(swlt, tmp_path, monkeypatch) -> None:
    from catalpa_tooling.fcos import dbdump, fetch
    from catalpa_tooling.fcos import transfer as t

    log = []
    r = Recorder()
    src, dst = _backend(swlt, r), _backend(swlt, r)
    monkeypatch.setattr(fetch, "fetch_db", lambda c, b, dest: log.append("dump") or dest.write_bytes(b"PGDMP"))
    monkeypatch.setattr(dbdump, "restore_archive", lambda b, p, **k: log.append(("restore", k)) or 0)
    monkeypatch.setattr(dbdump, "post_restore_hooks", lambda b: log.append("hooks") or 0)
    monkeypatch.setattr(type(src), "pull_media", lambda self, d: log.append("pull") or 0)
    monkeypatch.setattr(type(src), "push_media", lambda self, d: log.append("push") or 0)
    monkeypatch.setattr(type(src), "stop_writers", lambda self: log.append("stop") or ["django.service"])
    monkeypatch.setattr(type(src), "start_units", lambda self, u: log.append(("start", tuple(u))) or 0)
    kw = dict(src="a", dst="b", src_b=src, dst_b=dst, do_db=True, do_media=True, media_method="rsync",
              dry=False, yes=True, workdir=str(tmp_path), keep_workdir=False)  # fmt: skip
    assert t.run_transfer(swlt, **kw) == 0
    assert log == [
        "stop", "dump", ("restore", {"stop_app": False, "run_hooks": False}), "pull", "push",
        ("start", ("django.service",)), "hooks",
    ]  # fmt: skip
    assert not list(tmp_path.glob("a_to_b_*"))  # session directory removed

    # A failed step still brings the destination's writers back.
    log.clear()
    monkeypatch.setattr(type(src), "push_media", lambda self, d: log.append("push") or 23)
    assert t.run_transfer(swlt, **kw) == 23
    assert log[-1] == ("start", ("django.service",)) and "hooks" not in log
    assert t.run_transfer(swlt, **{**kw, "media_method": "tar"}) == 1


def test_push_media_chowns_even_when_rsync_is_interrupted(swlt, tmp_path, monkeypatch) -> None:
    r = Recorder()
    b = _backend(swlt, r)

    def interrupted(cmd, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr("catalpa_tooling.run_cmd.run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        b.push_media(tmp_path)
    assert r.calls[-1][-1].startswith("podman unshare chown -R ")
