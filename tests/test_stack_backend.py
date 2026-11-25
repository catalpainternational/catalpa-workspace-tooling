"""``stack_backend``: one interface over a compose env and a quadlet env (M5, step 1).

The compose side only delegates, so these check it calls today's functions with the env's
context; the quadlet side is ``fcos.backend.Backend`` with SSH faked.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from catalpa_tooling import compose, dk_transfer, media_pull, pgbackrest_db
from catalpa_tooling.config import load_project_config
from catalpa_tooling.fcos import backend
from catalpa_tooling.managed_deploy_env import ManagedDeployContext
from catalpa_tooling.stack_backend import BackendError, ComposeBackend, StackBackend, backend_for

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"
INFO = {"docker_host": "ssh://deploy@staging.site.example/run/user/1001/podman/podman.sock"}


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
    def remotes(self) -> list[list[str]]:
        return [shlex.split(c[-1]) for c in self.calls]


def quadlet(site) -> tuple[backend.Backend, Recorder]:
    r = Recorder()
    return backend.open_backend(site, "staging", INFO, runner=r, register_key=False), r


def compose_ctx(config, env_name: str = "prod") -> ManagedDeployContext:
    return ManagedDeployContext(
        env_name=env_name,
        compose_file="docker/compose.yml",
        env_add={"DOCKER_HOST": "ssh://deploy@prod.example"},
        docker_host="ssh://deploy@prod.example",
        dc_backup_docker_host="",
        site_origin="https://prod.example",
        site_origins=("https://prod.example",),
        use_prepulled_registry=True,
        image_registry="ghcr.io/example",
        info_tag="v1",
        config=config,
        storage_volumes={},
        info={"docker_host": "ssh://deploy@prod.example"},
    )


# --- both backends --------------------------------------------------------------------------


def test_both_backends_are_stack_backends(site) -> None:
    b, _ = quadlet(site)
    c = ComposeBackend(compose_ctx(site))
    for x, runtime in ((b, "quadlet"), (c, "compose")):
        assert isinstance(x, StackBackend)
        assert x.runtime == runtime
        assert x.env_name in x.describe()


# --- quadlet --------------------------------------------------------------------------------


def test_quadlet_ensure_db_running_starts_its_unit(site) -> None:
    b, r = quadlet(site)
    assert b.ensure_db_running() == 0
    assert r.remotes == [["systemctl", "--user", "start", "db.service"]]


def test_quadlet_writers_round_trip(site) -> None:
    b, r = quadlet(site)
    token = b.stop_writers()
    assert "db.service" not in token
    assert b.start_writers(token) == 0
    stop, start = r.remotes
    assert stop[:3] == ["systemctl", "--user", "stop"] and stop[3] == "caddy.socket"
    assert start[:3] == ["systemctl", "--user", "start"] and start[-1] == "caddy.socket"
    assert sorted(stop[3:]) == sorted(start[3:])


def test_quadlet_start_writers_with_nothing_stopped(site) -> None:
    b, r = quadlet(site)
    assert b.start_writers([]) == 0
    assert r.calls == []


# --- compose: delegates to today's functions ------------------------------------------------


def test_compose_ensure_db_running(site, monkeypatch) -> None:
    seen = {}

    def fake(compose_file, env, *, config=None, dk_env_name=None):
        seen.update(compose_file=compose_file, env=env, config=config, env_name=dk_env_name)
        return 3

    monkeypatch.setattr(pgbackrest_db, "ensure_db_service_running", fake)
    ctx = compose_ctx(site)
    assert ComposeBackend(ctx).ensure_db_running() == 3
    env = {**ctx.env_add, "COMPOSE_PROJECT_NAME": "site"}  # as `dk prod` sets it
    assert seen == {"compose_file": ctx.compose_file, "env": env, "config": site, "env_name": "prod"}


def test_compose_writers_go_through_transfers_functions(site, monkeypatch) -> None:
    calls = []

    def stop(f, e, *, dry_run):
        calls.append(("stop", f, dry_run))

    def start(f, e, *, dry_run, config, env_name, info):
        calls.append(("start", f, env_name, info["docker_host"]))

    monkeypatch.setattr(dk_transfer, "_stop_dest_writers", stop)
    monkeypatch.setattr(dk_transfer, "_start_dest_writers", start)
    c = ComposeBackend(compose_ctx(site))
    assert c.start_writers(c.stop_writers()) == 0
    assert calls == [
        ("stop", "docker/compose.yml", False),
        ("start", "docker/compose.yml", "prod", "ssh://deploy@prod.example"),
    ]


def test_compose_manage_execs_in_web(site, monkeypatch) -> None:
    seen = {}

    def fake_run(cmd, *, check, env, print_cmd, **kw):
        seen.update(cmd=cmd, check=check, docker_host=env.get("DOCKER_HOST"))
        return subprocess.CompletedProcess(cmd, 2)

    monkeypatch.setattr(compose, "run_cmd", fake_run)
    assert ComposeBackend(compose_ctx(site)).manage(["migrate"], user=None, tty=False) == 2
    web = site.stack_service("web")
    assert seen["cmd"][:4] == ["docker", "compose", "-f", "docker/compose.yml"]
    assert seen["cmd"][4:6] == ["exec", "-T"]
    assert seen["cmd"][-3:] == [web, "./manage.py", "migrate"]
    assert seen["check"] is False
    assert seen["docker_host"] == "ssh://deploy@prod.example"


class MediaRoute:
    """``ComposeBackend.pull_media``'s collaborators, faked: where rsync can read the media
    (``source``), what rsync returns, and the tar copy."""

    def __init__(self, monkeypatch, *, source: str | None, rsync_rc: int = 0) -> None:
        from catalpa_tooling import media_rsync
        from catalpa_tooling.media_storage import MediaStorage, MediaStorageKind

        self.calls: list[tuple] = []
        storage = MediaStorage(MediaStorageKind.VOLUME, "site_django_media")
        monkeypatch.setattr(ComposeBackend, "media_storage", lambda self: storage)
        monkeypatch.setattr(media_rsync, "resolve_rsync_endpoint", lambda env, st, **k: source)

        def rsync(src, dst, *, delete, dry_run):
            self.calls.append(("rsync", src, dst, delete, dry_run))
            return rsync_rc

        def tar(env, *, target, dry_run, alpine_image, config, storage):
            self.calls.append(("tar", target, storage.location))
            return 0

        monkeypatch.setattr(media_rsync, "rsync_between_endpoints", rsync)
        monkeypatch.setattr(media_pull, "run_pull_media", tar)


def test_compose_pull_media_rsyncs_when_it_can(site, monkeypatch, tmp_path) -> None:
    m = MediaRoute(monkeypatch, source="deploy@prod.example:/var/lib/docker/volumes/site_django_media/_data")
    assert ComposeBackend(compose_ctx(site)).pull_media(tmp_path / "m") == 0
    assert m.calls == [
        ("rsync", "deploy@prod.example:/var/lib/docker/volumes/site_django_media/_data", str(tmp_path / "m"), False, False)
    ]


def test_compose_pull_media_falls_back_to_tar(site, monkeypatch, tmp_path) -> None:
    m = MediaRoute(monkeypatch, source="deploy@prod.example:/v", rsync_rc=23)  # e.g. permission denied
    assert ComposeBackend(compose_ctx(site)).pull_media(tmp_path) == 0
    assert [c[0] for c in m.calls] == ["rsync", "tar"]


def test_compose_pull_media_tars_without_an_rsync_route(site, monkeypatch, tmp_path) -> None:
    m = MediaRoute(monkeypatch, source=None)  # a local engine whose volumes the host can't read
    assert ComposeBackend(compose_ctx(site)).pull_media(tmp_path) == 0
    assert m.calls == [("tar", tmp_path, "site_django_media")]


def test_compose_pull_media_dry_run_doesnt_fall_back(site, monkeypatch, tmp_path) -> None:
    m = MediaRoute(monkeypatch, source="deploy@prod.example:/v", rsync_rc=23)
    assert ComposeBackend(compose_ctx(site)).pull_media(tmp_path / "m", dry_run=True) == 23
    assert [c[0] for c in m.calls] == ["rsync"] and not (tmp_path / "m").exists()


def test_a_local_compose_env_uses_its_own_project(site) -> None:
    ctx = compose_ctx(site, "dev")
    ctx = replace(ctx, env_add={"DOCKER_HOST": ""}, docker_host="")
    assert ComposeBackend(ctx).env["COMPOSE_PROJECT_NAME"] == "site_dev"


def test_quadlet_pull_media_rsyncs_the_media_directory(site, monkeypatch, tmp_path) -> None:
    from catalpa_tooling import media_rsync

    b, _ = quadlet(site)
    calls = []
    monkeypatch.setattr(media_rsync, "rsync_between_endpoints", lambda *a, **k: calls.append((a, k)) or 0)
    assert b.media_rsync_source() == "deploy@staging.site.example:/var/mnt/data/django_media"
    assert b.pull_media(tmp_path / "m") == 0
    assert calls == [
        (("deploy@staging.site.example:/var/mnt/data/django_media", str(tmp_path / "m")), {"delete": False, "dry_run": False})
    ]


# --- backend_for ----------------------------------------------------------------------------


def test_backend_for_a_quadlet_env(site, monkeypatch) -> None:
    from catalpa_tooling import ssh_known_hosts

    monkeypatch.setattr(ssh_known_hosts, "ensure_known_host", lambda *a, **k: 0)
    b = backend_for(site, "staging")
    assert isinstance(b, backend.Backend)


def test_backend_for_a_compose_env(site, monkeypatch) -> None:
    from catalpa_tooling import managed_deploy_env

    env = site.deploy_envs_dir / "prod"
    env.mkdir()
    (env / "info.yaml").write_text("docker_host: ssh://deploy@prod.example\n", encoding="utf-8")
    ctx = compose_ctx(site)
    monkeypatch.setattr(managed_deploy_env, "load_managed_deploy_context", lambda config, name: ctx)
    b = backend_for(site, "prod")
    assert isinstance(b, ComposeBackend) and b.ctx is ctx


def test_backend_for_a_compose_env_without_context(site, monkeypatch) -> None:
    from catalpa_tooling import managed_deploy_env

    monkeypatch.setattr(managed_deploy_env, "load_managed_deploy_context", lambda config, name: None)
    with pytest.raises(BackendError):
        backend_for(site, "nope")


# --- dump_db and dk fetch db (step 2) -------------------------------------------------------


def test_quadlet_dump_db_returns_the_fetchs_exit_code(site, tmp_path, monkeypatch) -> None:
    from catalpa_tooling.fcos import fetch

    b, _ = quadlet(site)
    seen = {}

    def ok(config, q, *, dest, pg_dump_args, database):
        seen.update(q=q, dest=dest, args=pg_dump_args, database=database)

    monkeypatch.setattr(fetch, "fetch_db", ok)
    assert b.dump_db(tmp_path / "x.dump", ["-x"], database="metabase") == 0
    assert seen == {"q": b, "dest": tmp_path / "x.dump", "args": ["-x"], "database": "metabase"}

    def fails(config, q, **kw):
        raise SystemExit(1)

    monkeypatch.setattr(fetch, "fetch_db", fails)
    assert b.dump_db(tmp_path / "x.dump") == 1


def test_compose_dump_db_rejects_a_dump_too_small(site, tmp_path, monkeypatch) -> None:
    dest = tmp_path / "app.dump"
    seen = {}

    def fake(compose_file, env, d, extra, *, database=None):
        seen.update(extra=extra, database=database)
        d.write_bytes(b"PGDMP" + b"\0" * size)
        return 0

    monkeypatch.setattr(pgbackrest_db, "run_pg_dump_to_file", fake)
    c = ComposeBackend(compose_ctx(site))
    size = 200_000
    assert c.dump_db(dest) == 0
    assert seen == {"extra": None, "database": None}
    size = 10
    assert c.dump_db(dest, ["-x"], database="metabase") == 1
    assert seen == {"extra": ["-x"], "database": "metabase"}


def _fetch_dk(config, tmp_path, key: str = "app") -> None:
    from catalpa_tooling.config import FetchDatabaseEntry
    from catalpa_tooling.fetch_db import run_fetch_database

    entry = FetchDatabaseEntry(db_name="catalpa_db", via="dk")
    run_fetch_database(config, key, entry, dest=tmp_path / "app.dump", default_dk_env="staging")


def test_fetch_db_via_dk_starts_db_then_dumps(site, tmp_path, monkeypatch) -> None:
    from catalpa_tooling import stack_backend

    calls = []

    class Fake:
        def ensure_db_running(self):
            calls.append("ensure")
            return 0

        def dump_db(self, dest, args, *, database):
            calls.append(("dump", database))
            return 0

    monkeypatch.setattr(stack_backend, "backend_for", lambda config, env: Fake())
    _fetch_dk(site, tmp_path)
    assert calls == ["ensure", ("dump", None)]


def test_fetch_db_via_dk_stops_when_db_wont_start(site, tmp_path, monkeypatch) -> None:
    from catalpa_tooling import stack_backend

    class Fake:
        def ensure_db_running(self):
            return 4

        def dump_db(self, *a, **k):
            pytest.fail("dumped without a db")

    monkeypatch.setattr(stack_backend, "backend_for", lambda config, env: Fake())
    with pytest.raises(SystemExit) as exc:
        _fetch_dk(site, tmp_path)
    assert exc.value.code == 4


def test_fetch_db_via_dk_reports_an_env_it_cant_load(site, tmp_path, monkeypatch, capsys) -> None:
    from catalpa_tooling import stack_backend

    def fails(config, env):
        raise stack_backend.BackendError("staging: no deploy context")

    monkeypatch.setattr(stack_backend, "backend_for", fails)
    with pytest.raises(SystemExit):
        _fetch_dk(site, tmp_path)
    assert "fetch db: staging: no deploy context" in capsys.readouterr().err
