"""Tests for ``dk transfer``."""

import argparse
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from catalpa_tooling.dk_transfer import (
    _collect_transfer_preflight_errors,
    _dest_writer_services,
    cmd_transfer,
)
from catalpa_tooling.managed_deploy_env import ManagedDeployContext
from catalpa_tooling.media_storage import MediaStorage, MediaStorageKind


def _vol(name: str) -> MediaStorage:
    return MediaStorage(MediaStorageKind.VOLUME, name)


def _bind(path: str) -> MediaStorage:
    return MediaStorage(MediaStorageKind.BIND, path)


def _minimal_ctx(env_name: str, compose_file: str, minimal_project) -> ManagedDeployContext:
    return ManagedDeployContext(
        env_name=env_name,
        compose_file=compose_file,
        env_add={},
        docker_host="",
        dc_backup_docker_host="",
        site_origin="",
        site_origins=(),
        use_prepulled_registry=False,
        image_registry="",
        info_tag=None,
        config=minimal_project,
        storage_volumes={},
    )


def test_transfer_rejects_identical_environments(minimal_project) -> None:
    ns = argparse.Namespace(
        source_env="staging",
        dest_env="staging",
        dry_run=False,
        yes=True,
        db=False,
        media=False,
        workdir=None,
        keep_workdir=False,
    )
    assert cmd_transfer(ns, minimal_project) == 1


def test_dest_writer_services_only_django_when_compose_has_no_caddy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "db\ndjango\nfrontend\n"
        return m

    monkeypatch.setattr("catalpa_tooling.dk_transfer.run_cmd", fake_run)
    assert _dest_writer_services("compose.dev.yaml", {}) == ["django"]


def test_dest_writer_services_django_and_caddy_when_both_in_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "db\ndjango\ncaddy\n"
        return m

    monkeypatch.setattr("catalpa_tooling.dk_transfer.run_cmd", fake_run)
    assert _dest_writer_services("compose.yml", {}) == ["django", "caddy"]


def test_preflight_empty_when_compose_db_and_volumes_ok(
    monkeypatch: pytest.MonkeyPatch,
    minimal_project,
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        if "config" in cmd and "--services" in cmd:
            m.stdout = "db\ndjango\n"
        elif cmd[:3] == ["docker", "volume", "inspect"]:
            m.stdout = "[]"
        else:
            m.stdout = ""
        return m

    monkeypatch.setattr("catalpa_tooling.dk_transfer.run_cmd", fake_run)
    monkeypatch.setattr("catalpa_tooling.dk_transfer.db_service_responds", lambda c, e: True)
    errs = _collect_transfer_preflight_errors(
        src="local",
        dst="dev",
        src_ctx=_minimal_ctx("local", "compose.yml", minimal_project),
        dst_ctx=_minimal_ctx("dev", "compose.dev.yaml", minimal_project),
        src_r={},
        dst_r={},
        src_media=_vol("app_compose_local_django_media"),
        dst_media=_vol("app_compose_dev_django_media"),
        do_db=True,
        do_media=True,
        config=minimal_project,
    )
    assert errs == []


def test_preflight_errors_when_compose_config_fails(
    monkeypatch: pytest.MonkeyPatch,
    minimal_project,
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        if "config" in cmd and "--services" in cmd:
            m.returncode = 1
            m.stdout = ""
            m.stderr = "broken compose"
        else:
            m.returncode = 0
            m.stdout = ""
        return m

    monkeypatch.setattr("catalpa_tooling.dk_transfer.run_cmd", fake_run)
    errs = _collect_transfer_preflight_errors(
        src="local",
        dst="dev",
        src_ctx=_minimal_ctx("local", "compose.yml", minimal_project),
        dst_ctx=_minimal_ctx("dev", "compose.dev.yaml", minimal_project),
        src_r={},
        dst_r={},
        src_media=None,
        dst_media=None,
        do_db=False,
        do_media=False,
        config=minimal_project,
    )
    assert len(errs) == 2
    assert all("cannot list compose services" in e for e in errs)


def test_preflight_errors_when_no_db_service(
    monkeypatch: pytest.MonkeyPatch,
    minimal_project,
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "django\nredis\n"
        return m

    monkeypatch.setattr("catalpa_tooling.dk_transfer.run_cmd", fake_run)
    errs = _collect_transfer_preflight_errors(
        src="local",
        dst="dev",
        src_ctx=_minimal_ctx("local", "compose.yml", minimal_project),
        dst_ctx=_minimal_ctx("dev", "compose.dev.yaml", minimal_project),
        src_r={},
        dst_r={},
        src_media=None,
        dst_media=None,
        do_db=True,
        do_media=False,
        config=minimal_project,
    )
    assert len(errs) == 2
    assert all("no `db` service" in e for e in errs)


def test_preflight_errors_when_volume_missing(
    monkeypatch: pytest.MonkeyPatch,
    minimal_project,
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.stdout = "db\n"
        if "config" in cmd and "--services" in cmd:
            m.returncode = 0
            m.stdout = "db\ndjango\n"
        elif cmd[:3] == ["docker", "volume", "inspect"]:
            m.returncode = 1
        else:
            m.returncode = 0
        return m

    monkeypatch.setattr("catalpa_tooling.dk_transfer.run_cmd", fake_run)
    monkeypatch.setattr("catalpa_tooling.pgbackrest_volume_config.run_cmd", fake_run)
    monkeypatch.setattr("catalpa_tooling.dk_transfer.db_service_responds", lambda c, e: True)
    errs = _collect_transfer_preflight_errors(
        src="local",
        dst="dev",
        src_ctx=_minimal_ctx("local", "compose.yml", minimal_project),
        dst_ctx=_minimal_ctx("dev", "compose.dev.yaml", minimal_project),
        src_r={},
        dst_r={},
        src_media=_vol("missing_a"),
        dst_media=_vol("missing_b"),
        do_db=False,
        do_media=True,
        config=minimal_project,
    )
    assert len(errs) == 2
    assert all("not found" in e for e in errs)


def test_preflight_bind_source_skips_named_volume_ensure(
    monkeypatch: pytest.MonkeyPatch,
    minimal_project,
    tmp_path,
) -> None:
    media_src = tmp_path / "media"
    media_src.mkdir()
    (media_src / "file.txt").write_text("x", encoding="utf-8")
    media_dst = tmp_path / "out_media"
    media_dst.mkdir()

    ensure_calls: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        if "config" in cmd and "--services" in cmd:
            m.stdout = "db\ndjango\n"
        else:
            m.stdout = ""
        return m

    def fake_ensure(*args, **kwargs):
        ensure_calls.append("ensure")
        return 0

    monkeypatch.setattr("catalpa_tooling.dk_transfer.run_cmd", fake_run)
    monkeypatch.setattr(
        "catalpa_tooling.dk_transfer._ensure_stack_volumes",
        lambda **kwargs: ensure_calls.append(kwargs.get("env_name", "")) or None,
    )
    errs = _collect_transfer_preflight_errors(
        src="dev",
        dst="full",
        src_ctx=_minimal_ctx("dev", "compose.dev.yml", minimal_project),
        dst_ctx=_minimal_ctx("full", "compose.yml", minimal_project),
        src_r={},
        dst_r={},
        src_media=_bind(str(media_src)),
        dst_media=_bind(str(media_dst)),
        do_db=False,
        do_media=True,
        config=minimal_project,
    )
    assert errs == []
    assert ensure_calls == []


def test_preflight_empty_bind_source_errors(
    monkeypatch: pytest.MonkeyPatch,
    minimal_project,
    tmp_path,
) -> None:
    empty = tmp_path / "empty_media"
    empty.mkdir()
    dest = tmp_path / "dest_media"
    dest.mkdir()

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        m = MagicMock()
        m.returncode = 0
        m.stdout = "db\ndjango\n"
        return m

    monkeypatch.setattr("catalpa_tooling.dk_transfer.run_cmd", fake_run)
    errs = _collect_transfer_preflight_errors(
        src="dev",
        dst="full",
        src_ctx=_minimal_ctx("dev", "compose.dev.yml", minimal_project),
        dst_ctx=_minimal_ctx("full", "compose.yml", minimal_project),
        src_r={},
        dst_r={},
        src_media=_bind(str(empty)),
        dst_media=_bind(str(dest)),
        do_db=False,
        do_media=True,
        config=minimal_project,
    )
    assert len(errs) == 1
    assert "empty" in errs[0]
    assert str(empty) in errs[0]


def test_start_dest_writers_passes_local_proxy_override(
    monkeypatch: pytest.MonkeyPatch,
    minimal_project,
    tmp_path: Path,
) -> None:
    """Regression: transfer compose up must reset host ports behind catalpa-local-proxy."""
    from catalpa_tooling.dk_transfer import _start_dest_writers

    override = tmp_path / "marktwain-full.yaml"
    override.write_text("services:\n  caddy:\n    ports: !reset []\n", encoding="utf-8")
    sync_calls: list[tuple] = []
    compose_kwargs: list[dict] = []

    monkeypatch.setattr(
        "catalpa_tooling.dk_transfer.sync_local_proxy_for_compose_action",
        lambda *a, **k: sync_calls.append((a, k)) or 0,
    )
    monkeypatch.setattr(
        "catalpa_tooling.dk_transfer.local_proxy_extra_compose_files",
        lambda *a, **k: [str(override)],
    )
    monkeypatch.setattr(
        "catalpa_tooling.dk_transfer.dc_backup_tls_extra_compose_files",
        lambda *a, **k: [],
    )

    def fake_compose(compose_file, *args, **kwargs):
        compose_kwargs.append({"compose_file": compose_file, "args": args, **kwargs})
        m = MagicMock()
        m.returncode = 0
        return m

    monkeypatch.setattr("catalpa_tooling.dk_transfer._compose", fake_compose)

    info = {
        "compose_file": "compose.yaml",
        "local_proxy": {"lan_access": True},
        "env": {"compose_project_name": "tempu-local"},
    }
    _start_dest_writers(
        "compose.yaml",
        {"COMPOSE_PROJECT_NAME": "tempu-local", "DOCKER_HOST": ""},
        dry_run=False,
        config=minimal_project,
        env_name="full",
        info=info,
    )

    assert sync_calls, "expected local proxy sync before compose up"
    assert len(compose_kwargs) == 1
    assert compose_kwargs[0]["args"] == ("up", "-d")
    assert compose_kwargs[0]["extra_compose_files"] == [str(override)]


def test_transfer_invokes_post_db_restore_hooks_after_db_leg(
    monkeypatch: pytest.MonkeyPatch,
    minimal_project,
) -> None:
    hooks_envs: list[str] = []

    def fake_hooks(config, *, compose_file, env_add, env_name, dry_run=False):
        hooks_envs.append(env_name)
        return 0

    src_ctx = _minimal_ctx("local", "compose.yml", minimal_project)
    dst_ctx = _minimal_ctx("dev", "compose.dev.yaml", minimal_project)

    monkeypatch.setattr(
        "catalpa_tooling.dk_transfer.list_deploy_env_names",
        lambda _d: ["local", "dev"],
    )
    monkeypatch.setattr(
        "catalpa_tooling.dk_transfer.load_managed_deploy_context",
        lambda _cfg, name: src_ctx if name == "local" else dst_ctx,
    )
    monkeypatch.setattr(
        "catalpa_tooling.dk_transfer.resolve_env_with_compose_project",
        lambda compose_file, env_add, **kwargs: dict(env_add),
    )
    monkeypatch.setattr(
        "catalpa_tooling.dk_transfer._collect_transfer_preflight_errors",
        lambda **kwargs: [],
    )
    monkeypatch.setattr(
        "catalpa_tooling.dk_transfer._confirm_transfer_overwrite",
        lambda *a, **k: True,
    )
    monkeypatch.setattr("catalpa_tooling.dk_transfer._stop_dest_writers", lambda *a, **k: None)
    monkeypatch.setattr("catalpa_tooling.dk_transfer._start_dest_writers", lambda *a, **k: None)
    monkeypatch.setattr("catalpa_tooling.dk_transfer.run_pg_dump_to_file", lambda *a, **k: 0)
    monkeypatch.setattr("catalpa_tooling.dk_transfer.run_drop_create_app_database", lambda *a, **k: 0)
    monkeypatch.setattr("catalpa_tooling.dk_transfer.run_pg_restore", lambda *a, **k: 0)
    monkeypatch.setattr(
        "catalpa_tooling.dk_transfer.run_post_db_restore_manage_commands",
        fake_hooks,
    )

    ns = argparse.Namespace(
        source_env="local",
        dest_env="dev",
        dry_run=False,
        yes=True,
        db=True,
        media=False,
        workdir=None,
        keep_workdir=False,
    )
    assert cmd_transfer(ns, minimal_project) == 0
    assert hooks_envs == ["dev"]
