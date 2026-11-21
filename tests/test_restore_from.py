"""Tooling 2.0 M5b (#204): ``restore_from`` envs. The read key (``dk <env> secrets read-key``),
read mode in a rendered env, and the quadlet backend restoring from it. doctl, sops and SSH are
faked."""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from catalpa_tooling import restore_from as rf
from catalpa_tooling.config import load_project_config
from tests.test_fcos_render import _fake_adapter, needs_compose

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"

PROD = {
    "pgbr_s3_write_bucket": "swlt-backups",
    "pgbr_s3_write_region": "sgp1",
    "pgbr_s3_write_endpoint": "sgp1.digitaloceanspaces.com",
    "pgbr_s3_write_repo_path": "/swlt/prod/pgbackrest",
    "pgbr_s3_write_stanza": "main",
    "pgbr_s3_write_key": "PROD-KEY",
    "pgbr_s3_write_secret": "PROD-SECRET",
    "restic_write_repository": "s3:https://sgp1.digitaloceanspaces.com/swlt-media/restic",
    "restic_write_password": "PROD-RESTIC-PASSWORD",
    "restic_write_s3_access_key_id": "PROD-KEY",
    "restic_write_s3_secret_access_key": "PROD-SECRET",
}


# --- the source's settings ----------------------------------------------------------------------


def test_read_values_copy_the_settings_not_the_key() -> None:
    values, buckets = rf.read_values(PROD, source="prod")
    assert buckets == ["swlt-backups", "swlt-media"]
    assert values["pgbr_s3_read_repo_path"] == "/swlt/prod/pgbackrest"
    assert values["restic_read_password"] == "PROD-RESTIC-PASSWORD"  # needed to read the repo
    assert not any("PROD-KEY" in v or "PROD-SECRET" in v for v in values.values())
    full = rf.with_key(values, "NEW", "NEWS")
    assert full["pgbr_s3_read_key"] == full["restic_read_s3_access_key_id"] == "NEW"


def test_a_source_without_backups_is_refused() -> None:
    with pytest.raises(rf.RestoreFromError, match="no backups to restore from"):
        rf.read_values({"django_secret_key": "x"}, source="dev")


@pytest.mark.parametrize(
    "repo,bucket",
    [("s3:https://sgp1.digitaloceanspaces.com/b/p", "b"), ("s3:sgp1.digitaloceanspaces.com/b", "b"),
     ("/srv/restic", None)],
)  # fmt: skip
def test_restic_bucket(repo, bucket) -> None:
    assert rf.restic_bucket(repo) == bucket


def test_restore_source_is_checked(minimal_project) -> None:
    (minimal_project.deploy_envs_dir / "prod").mkdir(parents=True, exist_ok=True)
    (minimal_project.deploy_envs_dir / "prod" / "info.yaml").write_text("name: prod\n")
    assert rf.restore_source(minimal_project, "fcos", {"restore_from": "prod"}) == "prod"
    assert rf.restore_source(minimal_project, "fcos", {}) is None
    for bad, msg in (("fcos", "itself"), ("nope", "not an env"), ("", "empty")):
        with pytest.raises(rf.RestoreFromError, match=msg):
            rf.restore_source(minimal_project, "fcos", {"restore_from": bad})


# --- dk <env> secrets read-key ------------------------------------------------------------------


@pytest.fixture
def project(minimal_project):
    envs = minimal_project.deploy_envs_dir
    for env, text in (("prod", "name: prod\n"), ("fcos", "name: fcos\nruntime: quadlet\nrestore_from: prod\n")):
        (envs / env).mkdir(parents=True, exist_ok=True)
        (envs / env / "info.yaml").write_text(text)
        (envs / env / "credentials.yaml").write_text("sops: {}\n")
    return minimal_project


@pytest.fixture
def fake_do(monkeypatch):
    """Records key creation and sops writes; ``own`` is fcos's decrypted credentials."""
    state: dict = {"own": {"django_secret_key": "x"}, "created": [], "set": None, "deleted": [], "sops_fails": False}

    def decrypt(path: Path) -> dict:
        return dict(PROD) if path.parent.name == "prod" else dict(state["own"]) if path.parent.name == "fcos" else {}

    state["decrypt"] = decrypt
    monkeypatch.setattr("catalpa_tooling.doctl_binary.ensure_doctl_available", lambda: None)
    monkeypatch.setattr("catalpa_tooling.doctl_spaces_provision._doctl_context", lambda config: None)

    def create(name, grants, **k):
        state["created"].append((name, grants))
        return "NEW-ACCESS", "NEW-SECRET"

    monkeypatch.setattr("catalpa_tooling.doctl_spaces_provision._create_spaces_key", create)
    monkeypatch.setattr(
        "catalpa_tooling.doctl_spaces_provision._delete_spaces_key",
        lambda access, **k: state["deleted"].append(access),
    )

    def apply(path, values):
        from catalpa_tooling.sops_credentials import SopsCommandError

        if state["sops_fails"]:
            raise SopsCommandError("nope", returncode=5)
        state["set"] = (path, values)

    monkeypatch.setattr("catalpa_tooling.sops_credentials.apply_credential_sets", apply)
    return state


def test_read_key_makes_a_read_only_key_and_writes_the_env_credentials(project, fake_do, capsys) -> None:
    assert rf.cmd_read_key(project, "fcos", dry_run=False, yes=True, decrypt=fake_do["decrypt"]) == 0
    assert fake_do["created"] == [
        ("swlt-backups-fcos-read", "bucket=swlt-backups;permission=read,bucket=swlt-media;permission=read")
    ]
    path, values = fake_do["set"]
    assert path == project.deploy_envs_dir / "fcos" / "credentials.yaml"
    assert values["pgbr_s3_read_key"] == "NEW-ACCESS" and values["restic_read_s3_secret_access_key"] == "NEW-SECRET"
    assert values["pgbr_s3_read_stanza"] == "main"
    assert not any(k.startswith(("pgbr_s3_write", "restic_write")) for k in values)
    err = capsys.readouterr().err
    assert "NEW-SECRET" not in err and "PROD-RESTIC-PASSWORD" not in err
    assert "`dk fcos render`" in err


def test_read_key_dry_run_and_refusals(project, fake_do, capsys) -> None:
    assert rf.cmd_read_key(project, "fcos", dry_run=True, yes=False, decrypt=fake_do["decrypt"]) == 0
    assert fake_do["created"] == [] and fake_do["set"] is None
    assert "would run doctl spaces keys create 'swlt-backups-fcos-read'" in capsys.readouterr().err

    fake_do["own"] = {"pgbr_s3_read_key": "old"}
    assert rf.cmd_read_key(project, "fcos", dry_run=False, yes=True, decrypt=fake_do["decrypt"]) == 1
    assert "already has read credentials" in capsys.readouterr().err

    assert rf.cmd_read_key(project, "prod", dry_run=False, yes=True, decrypt=fake_do["decrypt"]) == 1
    assert "no restore_from" in capsys.readouterr().err
    assert fake_do["created"] == []


def test_a_failed_sops_write_deletes_the_new_key(project, fake_do) -> None:
    fake_do["sops_fails"] = True
    assert rf.cmd_read_key(project, "fcos", dry_run=False, yes=True, decrypt=fake_do["decrypt"]) == 5
    assert fake_do["deleted"] == ["NEW-ACCESS"]


def test_dk_routes_secrets_read_key(project, monkeypatch) -> None:
    from catalpa_tooling.cli.dk_argv import normalize_dk_env_argv
    from catalpa_tooling.dk_cli import _parse_env_argv
    from catalpa_tooling.dk_parser import build_dk_parser
    from catalpa_tooling.env_handlers import handle_env_command

    seen = []
    monkeypatch.setattr(rf, "cmd_read_key", lambda config, env, **k: seen.append((env, k)) or 0)
    ns = _parse_env_argv(build_dk_parser(project), normalize_dk_env_argv(["fcos", "--dry-run", "secrets", "read-key"]))
    assert handle_env_command(ns, project) == 0
    assert seen == [("fcos", {"dry_run": True, "yes": False})]


# --- read mode in a rendered env, and restoring from it --------------------------------------------

READ_NAMES = [
    "pgbr_s3_read_bucket", "pgbr_s3_read_region", "pgbr_s3_read_endpoint", "pgbr_s3_read_key",
    "pgbr_s3_read_secret", "pgbr_s3_read_repo_path", "pgbr_s3_read_stanza",
    "restic_read_repository", "restic_read_password", "restic_read_s3_access_key_id",
    "restic_read_s3_secret_access_key",
]  # fmt: skip


def _read_mode_swlt(tmp_path: Path, *, both: bool = False) -> Path:
    root = tmp_path / "swlt"
    shutil.copytree(FIXTURES / "swlt", root, ignore=shutil.ignore_patterns("expected"))
    env = root / "docker" / "envs" / "staging"
    creds = env / "credentials.yaml"
    keep = [ln for ln in creds.read_text().splitlines() if both or not ln.startswith(("pgbr_s3_write", "restic_write"))]
    creds.write_text("\n".join(keep + [f"{k}: ENC[x]" for k in READ_NAMES]) + "\n")
    (env / "info.yaml").write_text((env / "info.yaml").read_text() + "restore_from: prod\n")
    (root / "docker" / "envs" / "prod").mkdir(exist_ok=True)
    (root / "docker" / "envs" / "prod" / "info.yaml").write_text("name: prod\nbackups: [db, files]\n")
    if not both:
        info = env / "info.yaml"
        info.write_text(info.read_text().replace("backups: [db, files]\n", ""))
    return root


@needs_compose
def test_read_mode_renders_restore_containers_and_no_backups(tmp_path) -> None:
    from catalpa_tooling.fcos.render import render_env

    config = load_project_config(_read_mode_swlt(tmp_path))
    render = render_env(config, "staging", adapter=_fake_adapter(["/entrypoint.sh"]))
    import json

    manifest = json.loads(render.files["manifest.json"])
    # No backup units, no archiving, no timers: nothing writes to the source's repository.
    assert not any("pgbackrest" in f or "restic" in f for f in render.files if f != "manifest.json")
    assert manifest["timers"] == []
    assert "PGBACKREST" not in render.files["quadlets/db.container"]
    restore = manifest["restore"]
    assert restore["from"] == "prod"
    assert "Secret=pgbr_s3_read_key,type=env,target=PGBACKREST_REPO1_S3_KEY" in restore["pgbackrest"]
    assert "Environment=PGBACKREST_REPO1_TYPE=s3" in restore["pgbackrest"]
    assert any(ln.startswith("UIDMap=0:") for ln in restore["restic"])
    assert "Secret=restic_read_password,type=env,target=RESTIC_PASSWORD" in restore["restic"]
    assert {k.upper() for k in READ_NAMES} <= set(manifest["secrets"].values())


@needs_compose
def test_read_and_write_in_one_rendered_env_is_refused(tmp_path) -> None:
    from catalpa_tooling.fcos.render import RenderError, render_env

    config = load_project_config(_read_mode_swlt(tmp_path, both=True))
    with pytest.raises(RenderError, match="backed up here and restored from prod"):
        render_env(config, "staging", adapter=_fake_adapter(["/entrypoint.sh"]))


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, input=None, tty=False):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")


@pytest.fixture
def read_env(tmp_path, monkeypatch):
    """The swlt fixture's committed render, in read mode."""
    from catalpa_tooling.fcos import backup_credentials
    from catalpa_tooling.fcos.render import render_env, write_render

    monkeypatch.setattr(backup_credentials, "ensure_backup_credentials", lambda *a, **k: 0)
    root = _read_mode_swlt(tmp_path)
    config = load_project_config(root)
    try:
        render = render_env(config, "staging", adapter=_fake_adapter(["/entrypoint.sh"]))
    except Exception as exc:  # noqa: BLE001  (no docker compose here)
        pytest.skip(f"render needs docker compose: {exc}")
    write_render(root / "docker" / "envs" / "staging" / "generated" / "fcos", render.files)
    return config


def sub(config, command: str, subcmd: str, **extra):
    from catalpa_tooling.fcos import backend

    r = Recorder()
    ns = argparse.Namespace(env_command=command, **{"dry_run": False, f"{command}_command": subcmd, **extra})
    info = {"docker_host": "ssh://deploy@staging.site.example/run/user/1001/podman/podman.sock"}
    return backend.handle(ns, config, "staging", info, runner=r, tty=False), r


def test_db_restore_uses_the_read_key_and_replays_wal_in_the_one_off(read_env, capsys) -> None:
    rc, r = sub(read_env, "db", "restore", pgbackrest_restore_args=["--", "--type=time", "--target=2026-11-19"], yes=False, dry_run=True)
    assert rc == 0 and r.calls == []
    script = capsys.readouterr().err.split(":\n", 1)[1]
    run_line = next(ln for ln in script.splitlines() if ln.startswith("podman run"))
    argv = shlex.split(run_line)
    assert "pgbr_s3_read_key,type=env,target=PGBACKREST_REPO1_S3_KEY" in argv
    assert "PGBACKREST_REPO1_TYPE=s3" in argv
    inner = argv[-1]
    assert "restore --delta --type=time --target=2026-11-19 --target-action=promote" in inner
    assert "pg_ctl" in inner and "pg_is_in_recovery()" in inner and "archive_mode=off" in inner


def test_read_mode_refuses_writes_and_reads_the_source(read_env, capsys) -> None:
    for command, subcmd, extra in (("db", "backup", {"backup_type": "incr"}), ("db", "check", {}),
                                   ("files", "backup", {}), ("files", "init", {})):  # fmt: skip
        rc, r = sub(read_env, command, subcmd, **extra)
        assert rc == 2 and r.calls == []
        err = capsys.readouterr().err
        assert "restores prod's backups" in err or "It restores prod's backups instead" in err
    rc, r = sub(read_env, "db", "info")
    cmd = shlex.split(r.calls[-1][-1])
    assert cmd[:7] == ["podman", "run", "--rm", "--user", "postgres", "--entrypoint", "pgbackrest"]
    assert cmd[-1] == "info" and "pgbr_s3_read_secret,type=env,target=PGBACKREST_REPO1_S3_KEY_SECRET" in cmd
    rc, r = sub(read_env, "files", "snapshots")
    cmd = shlex.split(r.calls[-1][-1])
    assert "restic_read_password,type=env,target=RESTIC_PASSWORD" in cmd
    assert cmd[-2:] == ["--no-lock", "snapshots"]


def test_files_restore_from_the_source_filters_by_path_not_host(read_env, capsys) -> None:
    rc, _r = sub(read_env, "files", "restore", snapshot="latest", yes=False, dry_run=True)
    assert rc == 0
    script = capsys.readouterr().err
    assert "--path /backup/" in script and "--host" not in script
    # The read key can't write restic's lock: --no-lock, between the image and the command.
    assert "--no-lock restore latest:/backup/" in script
    assert "--exclude-xattr security.selinux" in script
    # The edge (Caddy, its own range) mounts the media read-only: it must be able to read it.
    chmod = next(ln for ln in script.splitlines() if "--entrypoint chmod" in ln)
    assert chmod.endswith("-R o+rX /backup/django_media") and "--uidmap 0:" in chmod


@pytest.mark.parametrize("web_up", [True, False])
def test_post_db_restore_hooks_run_once_the_app_is_up(read_env, monkeypatch, capsys, web_up) -> None:
    """As compose does after a pgBackRest restore; a stack left stopped says what to run."""
    from catalpa_tooling import post_db_restore as pdr
    from catalpa_tooling.fcos import dbdump

    ran = []
    monkeypatch.setattr(pdr, "post_db_restore_applies", lambda config, env: True)
    monkeypatch.setattr(pdr, "_expand_commands", lambda *a, **k: [("sync_wagtail_sites", "--profile", "staging")])
    monkeypatch.setattr(dbdump, "post_restore_hooks", lambda b: ran.append(b.env_name) or 0)
    from catalpa_tooling.fcos import backend

    r = Recorder()

    def runner(argv, *, input=None, tty=False):
        r.calls.append(argv)
        rc = 0 if web_up or not argv[-1].startswith("systemctl --user is-active") else 3
        return subprocess.CompletedProcess(argv, rc, b"", b"")

    ns = argparse.Namespace(env_command="db", db_command="restore", pgbackrest_restore_args=[], yes=True, dry_run=False)
    info = {"docker_host": "ssh://deploy@staging.site.example/run/user/1001/podman/podman.sock"}
    assert backend.handle(ns, read_env, "staging", info, runner=runner, tty=False) == 0
    if web_up:
        assert ran == ["staging"]
    else:
        assert ran == [] and "`dk staging manage sync_wagtail_sites --profile staging`" in capsys.readouterr().err
