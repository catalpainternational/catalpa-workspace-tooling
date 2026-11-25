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
    assert script[1] == "web_was=0; systemctl --user is-active --quiet django.service && web_was=1"
    assert script[3] == "systemctl --user stop django.service db.service"
    run_ = shlex.split(script[4])
    assert run_[:7] == ["podman", "run", "--rm", "--user", "postgres", "--entrypoint", "sh"]
    # db's own settings, secrets and mounts, with quadlet volume units resolved to their names.
    assert "--env-file" in run_ and "--uidmap" in run_
    assert "/var/mnt/data/postgres_data:/var/lib/postgresql:Z" in run_
    assert run_[-1].endswith("restore --delta --type=time '--target=2026-11-19 10:00'")
    # Started again only if it was running: a --no-start host stays stopped for files restore.
    assert script[7] == "  systemctl --user start django.service"
    assert script[10] == "  systemctl --user start db.service"
    assert "stay so: restore the media (dk staging files restore)" in script[12]


def test_db_restore_leaves_a_stopped_stack_stopped(swlt, tmp_path) -> None:
    """Run the script's start/stop logic in sh with systemctl and podman faked."""
    import subprocess

    rc, r = run_sub(swlt, "db", "restore", pgbackrest_restore_args=[], yes=True)
    assert rc == 0
    script = r.calls[-1][-1]
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "log"
    (bin_ / "podman").write_text("#!/bin/sh\necho podman >> \"$LOG\"\n")
    (bin_ / "systemctl").write_text(
        '#!/bin/sh\necho "$*" >> "$LOG"\n'
        'case "$*" in *is-active*django*) [ -n "$WEB_UP" ];; *is-active*db*) [ -n "$DB_UP" ];; esac\n'
    )
    for f in bin_.iterdir():
        f.chmod(0o755)

    def run(**up: str) -> list[str]:
        log.write_text("")
        env = {"PATH": f"{bin_}:/usr/bin:/bin", "LOG": str(log), **up}
        assert subprocess.run(["sh", "-c", script], env=env, capture_output=True, check=False).returncode == 0
        return [line for line in log.read_text().splitlines() if line.startswith("--user start")]

    assert run() == []
    assert run(DB_UP="1") == ["--user start db.service"]
    assert run(WEB_UP="1", DB_UP="1") == ["--user start django.service"]


def test_db_restore_asks_first(swlt, monkeypatch) -> None:
    monkeypatch.setattr("catalpa_tooling.cli_confirm.confirm_by_typing_env_name", lambda env: False)
    rc, r = run_sub(swlt, "db", "restore", pgbackrest_restore_args=[], yes=False)
    assert rc == 1 and r.calls == []
    rc, r = run_sub(swlt, "db", "restore", pgbackrest_restore_args=[], yes=True)
    assert rc == 0 and "pgbackrest --log-level-console=info restore --delta" in r.calls[-1][-1]


def test_db_restore_file_dispatches(swlt, tmp_path, monkeypatch) -> None:
    from catalpa_tooling import db_archive

    seen = []
    monkeypatch.setattr(db_archive, "restore_archive", lambda b, path, *a, **k: seen.append(path) or 0)
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
        "--delete", "--host", "swlt-shape-staging", "--exclude-xattr", "security.selinux",
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

    from catalpa_tooling.db_archive import restore_archive

    dump = tmp_path / "app.dump"
    dump.write_bytes(b"PGDMP" + b"x" * 1000)
    r = Recorder()
    uploads = []
    b = _backend(swlt, r)
    b.uploader = lambda argv, path: uploads.append((argv, path)) or 0
    rc = restore_archive(b, dump, stop_app=True)
    assert rc == 0
    cmds = [c[-1] for c in r.calls]
    assert cmds[0].startswith("systemctl --user stop ") and "db.service" not in cmds[0]
    assert "django.service" in cmds[0]
    assert cmds[1] == "systemctl --user start db.service"  # db is up before anything is copied
    if b._proxy_socket():  # the edge's socket stops first and starts last
        assert cmds[0].split()[3] == b._proxy_socket()[0]
        assert cmds[6].endswith(b._proxy_socket()[0])
    (up_argv, up_path), = uploads
    assert up_path == dump and "podman exec -i -u root db sh -c" in up_argv[-1]
    remote = up_argv[-1].split("cat > ")[1].split("'")[0]
    assert hashlib.sha256(dump.read_bytes()).hexdigest() in cmds[2] and remote in cmds[2]
    assert "dropdb" in cmds[3] and "createdb" in cmds[3]
    assert "pg_restore" in cmds[4] and remote in cmds[4]
    assert cmds[5] == f"podman exec -u root -i db sh -c 'rm -f {remote}'"
    assert cmds[6].startswith("systemctl --user start ") and "django.service" in cmds[6]


def test_restore_archive_refuses_a_non_archive_and_cleans_up_on_failure(swlt, tmp_path) -> None:
    from catalpa_tooling.db_archive import restore_archive

    bad = tmp_path / "bad.dump"
    bad.write_bytes(b"not a dump")
    r = Recorder()
    assert restore_archive(_backend(swlt, r), bad, stop_app=True) == 1 and r.calls == []

    good = tmp_path / "app.dump"
    good.write_bytes(b"PGDMP" + b"x" * 10)

    class FailRestore(Recorder):
        def __call__(self, argv, *, input=None, tty=False):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 1 if "pg_restore" in argv[-1] else 0, b"", b"")

    r = FailRestore()
    b = _backend(swlt, r)
    b.uploader = lambda a, p: 0
    assert restore_archive(b, good, stop_app=True) == 1
    tail = [c[-1] for c in r.calls[-2:]]
    assert tail[0].startswith("podman exec -u root -i db sh -c 'rm -f ")  # archive removed
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


def test_pull_media_is_deprecated_and_fetches_media(swlt, monkeypatch, tmp_path, capsys) -> None:
    calls = []
    monkeypatch.setattr(
        "catalpa_tooling.media_rsync.rsync_between_endpoints", lambda src, dst, **k: calls.append((src, dst)) or 0
    )
    r = Recorder()
    ns = argparse.Namespace(env_command="pull_media", target=str(tmp_path / "m"), dry_run=False)
    info = {"docker_host": "ssh://deploy@staging.site.example/run/user/1001/podman/podman.sock"}
    assert backend.handle(ns, swlt, "staging", info, runner=r, tty=False) == 0
    assert calls == [("deploy@staging.site.example:/var/mnt/data/django_media", str((tmp_path / "m").resolve()))]
    assert "Deprecated: use `dk fetch media --env staging`" in capsys.readouterr().err


def test_quadlet_to_quadlet_transfer_order(swlt, tmp_path, monkeypatch) -> None:
    from catalpa_tooling import db_archive
    from catalpa_tooling.fcos import dbdump, fetch
    from catalpa_tooling.fcos import transfer as t

    log = []
    r = Recorder()
    src, dst = _backend(swlt, r), _backend(swlt, r)
    monkeypatch.setattr(fetch, "fetch_db", lambda c, b, dest: log.append("dump") or dest.write_bytes(b"PGDMP"))
    monkeypatch.setattr(db_archive, "restore_archive", lambda b, p, **k: log.append(("restore", k)) or 0)
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


# --- restore hooks (#209) -------------------------------------------------------------------


def _with_hooks(config, *, envs=None):
    from dataclasses import replace

    ops = replace(
        config.ops,
        post_db_restore=replace(
            config.ops.post_db_restore, envs=envs, db_psql=(), manage_commands=(("app-followups",),)
        ),
        post_metabase_db_restore=replace(
            config.ops.post_metabase_db_restore, envs=envs, db_psql=(),
            manage_commands=(("mb-followups", "--quiet"),), restart_services=("metabase",),
        ),
    )  # fmt: skip
    return replace(config, ops_optional=ops)


class _StackRecorder(Recorder):
    def __init__(self, active: bool) -> None:
        super().__init__()
        self.active = active

    def __call__(self, argv, *, input=None, tty=False):
        self.calls.append(argv)
        rc = 0 if self.active or "is-active" not in argv[-1] else 3
        return subprocess.CompletedProcess(argv, rc, b"", b"")


def test_pgbackrest_restore_runs_the_metabase_hooks_after_the_app_ones(swlt) -> None:
    """As compose does after a pgBackRest restore: post_db_restore, then post_metabase_db_restore
    (manage commands, then restart_services)."""
    r = _StackRecorder(active=True)
    b = _backend(_with_hooks(swlt), r)
    assert b._after_db_restore() == 0
    cmds = [c[-1] for c in r.calls]
    app = next(i for i, c in enumerate(cmds) if "app-followups" in c)
    mb = next(i for i, c in enumerate(cmds) if "mb-followups --quiet" in c)
    restart = cmds.index("systemctl --user restart metabase.service")
    assert app < mb < restart


def test_stopped_stack_lists_both_hook_sets(swlt, capsys) -> None:
    r = _StackRecorder(active=False)
    b = _backend(_with_hooks(swlt), r)
    assert b._after_db_restore() == 0
    assert not any("followups" in c[-1] for c in r.calls)
    err = capsys.readouterr().err
    assert "post_db_restore hooks not run" in err and "`dk staging manage app-followups`" in err
    assert "post_metabase_db_restore hooks not run" in err
    assert "`dk staging manage mb-followups --quiet`" in err and "`dk staging restart metabase`" in err


def test_hooks_for_other_envs_do_nothing(swlt) -> None:
    r = _StackRecorder(active=True)
    assert _backend(_with_hooks(swlt, envs=("prod",)), r)._after_db_restore() == 0
    assert r.calls == []


def test_dump_restore_runs_only_the_app_hooks(swlt, tmp_path) -> None:
    """A dump restore replaces only the app database: the Metabase hooks don't apply."""
    from catalpa_tooling.db_archive import restore_archive

    dump = tmp_path / "app.dump"
    dump.write_bytes(b"PGDMP" + b"x" * 100)
    r = _StackRecorder(active=True)
    b = _backend(_with_hooks(swlt), r)
    b.uploader = lambda argv, path: 0
    assert restore_archive(b, dump, stop_app=True) == 0
    cmds = " ".join(c[-1] for c in r.calls)
    assert "app-followups" in cmds and "mb-followups" not in cmds and "restart metabase" not in cmds


def test_restic_paths_include_ops_backup_path(swlt) -> None:
    """A compose host's snapshots keep the media at ops.restic.backup_path (bero:
    /mnt/btrfs-data/bero-media): the restore and the media check must use that path, not only
    /backup/<volume> (the media check queried an empty --path and reported no snapshot)."""
    from dataclasses import replace

    ops = replace(swlt.ops, restic=replace(swlt.ops.restic, backup_path="/mnt/btrfs-data/x-media"))
    b = _backend(replace(swlt, ops_optional=ops), Recorder())
    text = (
        "[Container]\nImage=docker.io/restic/restic:0.18.1\n"
        "Volume=/var/mnt/data/django_media:/mnt/btrfs-data/x-media:ro,z\n"
        "Volume=/var/mnt/data/other:/backup/other:ro,z\n"
        "Volume=/etc/x/conf:/etc/conf:ro\n"
    )
    assert b._restic_mounts(text) == {
        "/mnt/btrfs-data/x-media": "/var/mnt/data/django_media",
        "/backup/other": "/var/mnt/data/other",
    }
    # Without backup_path, only /backup/… counts.
    assert list(_backend(swlt, Recorder())._restic_mounts(text)) == ["/backup/other"]


def _backend(site, r):
    info = {"docker_host": "ssh://deploy@staging.site.example/run/user/1001/podman/podman.sock"}
    return backend.open_backend(site, "staging", info, runner=r, register_key=False)


def test_run_manage_once_starts_the_task_unit_with_a_per_run_drop_in(site) -> None:
    r = Recorder()
    rc = _backend(site, r).run_manage_once(["sync_wagtail_sites", "--origin=https://a.test/?x=1&y", "50%", "$HOME", 'it\'s "q"'])
    assert rc == 0
    script = r.calls[-1][-1]
    # The task unit, not django: the app need not be up.
    assert "systemctl --user start django-task.service" in script
    assert "systemctl --user start django.service" not in script
    # Quoted for systemd (`%` and `$` doubled, words with quotes in double quotes), as for `command:`.
    dropin = next(t for t in shlex.split(script) if t.startswith("[Container]"))
    assert dropin.splitlines()[1] == (
        'Exec=./manage.py sync_wagtail_sites --origin=https://a.test/?x=1&y 50%% $$HOME "it\'s \\"q\\""'
    )
    # Per run: in the account's runtime dir, under a lock, removed afterwards.
    assert "/run/user/$(id -u)/containers/systemd/django-task.container.d" in script
    assert "flock" in script and "rm -rf" in script and "trap" in script
    assert "User=" not in script


def test_run_manage_once_sets_the_user_when_one_is_given(site) -> None:
    r = Recorder()
    _backend(site, r).run_manage_once(["shell"], user="root")
    dropin = next(t for t in shlex.split(r.calls[-1][-1]) if t.startswith("[Container]"))
    assert dropin.splitlines()[2] == "User=root"


@pytest.mark.parametrize("bad", ["", "a\nb"])
def test_run_manage_once_refuses_an_argument_systemd_would_mangle(site, bad) -> None:
    r = Recorder()
    with pytest.raises(backend.Usage, match="empty or multi-line"):
        _backend(site, r).run_manage_once(["a", bad, "c"])
    assert r.calls == []  # nothing reached the host


def test_run_manage_once_needs_the_task_unit_in_the_render(site) -> None:
    b = _backend(site, Recorder())
    del b.committed.files["quadlets/django-task.container"]
    with pytest.raises(backend.Usage, match="render"):
        b.run_manage_once(["migrate"])


# --- refusing early when the hooks can't run -------------------------------------------------


def _without_task_unit(b):
    b.committed.files.pop("quadlets/django-task.container")
    return b


def test_check_hooks_runnable(swlt) -> None:
    hooked = _with_hooks(swlt)
    # Hooks apply and the render has the task unit: fine.
    _backend(hooked, Recorder()).check_hooks_runnable()
    # No hook applies to this env (or it has none): the task unit isn't needed.
    _without_task_unit(_backend(_with_hooks(swlt, envs=("prod",)), Recorder())).check_hooks_runnable()
    # Hooks apply, no task unit: refused, saying what to do.
    with pytest.raises(backend.Usage, match="render has no django-task.container"):
        _without_task_unit(_backend(hooked, Recorder())).check_hooks_runnable()


def test_dump_restore_refuses_before_touching_anything_without_the_task_unit(swlt, tmp_path) -> None:
    dump = tmp_path / "app.dump"
    dump.write_bytes(b"PGDMP" + b"x" * 100)
    r = Recorder()
    b = _without_task_unit(_backend(_with_hooks(swlt), r))
    with pytest.raises(backend.Usage, match="django-task.container"):
        b.db_restore_dump(dump, yes=True, dry_run=False)
    assert r.calls == []  # nothing stopped, nothing replaced


def test_transfer_preflight_refuses_a_destination_without_the_task_unit(swlt, tmp_path, capsys) -> None:
    from catalpa_tooling.fcos import transfer as t

    r = Recorder()
    src, dst = _backend(swlt, r), _without_task_unit(_backend(_with_hooks(swlt), r))
    kw = dict(src="a", dst="b", src_b=src, dst_b=dst, do_db=True, do_media=False, media_method="rsync",
              dry=False, yes=True, workdir=str(tmp_path), keep_workdir=False)  # fmt: skip
    assert t.run_transfer(_with_hooks(swlt), **kw) == 1
    assert r.calls == []
    assert "preflight failed" in capsys.readouterr().err


def _metabase_hooks_only(config):
    from dataclasses import replace

    hooked = _with_hooks(config)
    ops = replace(hooked.ops, post_db_restore=replace(hooked.ops.post_db_restore, manage_commands=()))
    return replace(hooked, ops_optional=ops)


def test_a_metabase_only_manage_hook_blocks_only_the_pgbackrest_paths(swlt, tmp_path) -> None:
    """The dump restore and transfer run only the app hooks, so a Metabase hook needing the task
    unit must not refuse them; a pgBackRest restore brings back Metabase's database too."""
    cfg = _metabase_hooks_only(swlt)
    b = _without_task_unit(_backend(cfg, Recorder()))
    b.check_hooks_runnable()  # the app hooks have no manage command
    with pytest.raises(backend.Usage, match="django-task.container"):
        b.check_hooks_runnable(metabase=True)

    dump = tmp_path / "app.dump"
    dump.write_bytes(b"PGDMP" + b"x" * 100)
    b = _without_task_unit(_backend(cfg, Recorder()))
    assert b.db_restore_dump(dump, yes=True, dry_run=True) == 0  # a clean dry run: not refused
    with pytest.raises(backend.Usage, match="django-task.container"):
        _without_task_unit(_backend(cfg, Recorder())).db_restore([], yes=True, dry_run=False)


def test_dry_run_prints_the_plan_and_reports_that_a_real_run_would_be_refused(swlt, tmp_path, capsys) -> None:
    dump = tmp_path / "app.dump"
    dump.write_bytes(b"PGDMP" + b"x" * 100)
    r = Recorder()
    b = _without_task_unit(_backend(_with_hooks(swlt), r))
    assert b.db_restore_dump(dump, yes=True, dry_run=True) == 1
    err = capsys.readouterr().err
    assert "dry-run: would replace" in err and "a real run would be refused" in err and "django-task.container" in err
    assert r.calls == []

    assert _without_task_unit(_backend(_with_hooks(swlt), Recorder())).db_restore([], yes=True, dry_run=True) == 1
    assert "dry-run: on " in capsys.readouterr().err  # the plan still printed
    # With the unit present a dry run is still clean.
    assert _backend(_with_hooks(swlt), Recorder()).db_restore_dump(dump, yes=True, dry_run=True) == 0
