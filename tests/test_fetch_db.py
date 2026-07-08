"""Tests for config-driven fetch_db and unified restore."""

from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.db_restore import (
    pgbackrest_restore_configured,
    run_compose_metabase_dump_restore,
    run_unified_db_restore,
)
from catalpa_tooling.fetch_db import dump_path_usable, run_fetch_all_dbs
from catalpa_tooling.pgbackrest_volume_config import PREFIX_READ


def _write_tooling(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("n=1\n", encoding="utf-8")
    dumps = tmp_path / "docker" / "dumps"
    dumps.mkdir(parents=True)
    (tmp_path / "tooling.yaml").write_text(
        """
project:
  name: testapp
paths:
  backend: backend
  frontend: frontend
  scripts: scripts
  env_local: .env.local
  email_backend_dir: email_out
  fetch_db_dump: docker/dumps/app_db.custom
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
stack:
  compose_project_default: testapp
  services: {web: w, proxy: p, db: d}
  images:
    registry_key: image_registry
    components: {web: w-img, proxy: p-img, db: d-img}
  healthcheck: {service: w, url: http://localhost/healthz}
ops:
  install_prefix: /opt/app
  config_dir: /etc/app
  systemd_unit_prefix: app-
  transfer_workdir: .xfer
  default_db_container: app_db
  pgbackrest:
    postgres_conf: a.conf
    pgbackrest_conf: b.conf
    default_registry: reg
    restore_temp_prefix: pre_
  zabbix:
    unit_name: u.service
    userparams_file: 99.conf
  systemd_units:
    pgbackrest: []
    restic: []
native:
  fetch:
    dk_env: prod
    ssh_host: root@example.com
    databases:
      app:
        db_name: app_db
        via: ssh_native
""",
        encoding="utf-8",
    )


def _valid_dump(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PGDMP" + b"x" * 200_000)


def test_dump_path_usable_rejects_small_file(tmp_path: Path) -> None:
    p = tmp_path / "small.custom"
    p.write_bytes(b"PGDMPtiny")
    assert dump_path_usable(p) is False


def test_dump_path_usable_accepts_custom_format(tmp_path: Path) -> None:
    p = tmp_path / "ok.custom"
    _valid_dump(p)
    assert dump_path_usable(p) is True


def test_run_fetch_ssh_native(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_tooling: None) -> None:
    _write_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    dest = cfg.fetch_db_dump_path

    class _Proc:
        returncode = 0
        stdout = b"PGDMP" + b"x" * 200_000
        stderr = b""

    monkeypatch.setattr("catalpa_tooling.fetch_db.ensure_ssh_known_host_for_ssh_target", lambda _t: 0)
    monkeypatch.setattr("catalpa_tooling.fetch_db.subprocess.run", lambda *a, **k: _Proc())
    run_fetch_all_dbs(cfg)
    assert dest.is_file()
    assert dump_path_usable(dest)


def test_pgbackrest_restore_configured_read_mode() -> None:
    env = {
        f"{PREFIX_READ}BUCKET": "b",
        f"{PREFIX_READ}REGION": "r",
        f"{PREFIX_READ}KEY": "k",
        f"{PREFIX_READ}SECRET": "s",
        f"{PREFIX_READ}REPO_PATH": "/p",
        f"{PREFIX_READ}STANZA": "main",
    }
    assert pgbackrest_restore_configured(env) is True


def test_unified_restore_force_dumps_missing_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_tooling: None,
) -> None:
    _write_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    rc = run_unified_db_restore(
        cfg,
        compose_file="compose.yml",
        env_add={},
        env_name="dev",
        force_dumps=True,
    )
    assert rc == 1


def test_unified_restore_auto_fetch_then_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_tooling: None,
) -> None:
    _write_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    _valid_dump(cfg.fetch_db_dump_path)

    monkeypatch.setattr(
        "catalpa_tooling.db_restore.pgbackrest_restore_configured",
        lambda _env: False,
    )
    monkeypatch.setattr(
        "catalpa_tooling.db_restore.run_compose_app_dump_restore",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        "catalpa_tooling.db_restore.run_compose_metabase_dump_restore",
        lambda *a, **k: 0,
    )

    rc = run_unified_db_restore(
        cfg,
        compose_file="compose.yml",
        env_add={},
        env_name="dev",
    )
    assert rc == 0


def test_unified_restore_auto_fetch_invokes_fetch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_tooling: None,
) -> None:
    _write_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    called: list[str] = []

    def _fake_fetch(config, **kwargs):
        called.append("fetch")
        _valid_dump(config.fetch_db_dump_path)
        return None

    monkeypatch.setattr(
        "catalpa_tooling.db_restore.pgbackrest_restore_configured",
        lambda _env: False,
    )
    monkeypatch.setattr("catalpa_tooling.db_restore.run_fetch_all_dbs", _fake_fetch)
    monkeypatch.setattr(
        "catalpa_tooling.db_restore.run_compose_app_dump_restore",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        "catalpa_tooling.db_restore.run_compose_metabase_dump_restore",
        lambda *a, **k: 0,
    )

    rc = run_unified_db_restore(
        cfg,
        compose_file="compose.yml",
        env_add={},
        env_name="dev",
    )
    assert rc == 0
    assert called == ["fetch"]


def test_metabase_dump_restore_runs_hooks_after_pgrestore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_tooling: None,
) -> None:
    _write_tooling(tmp_path)
    text = (tmp_path / "tooling.yaml").read_text(encoding="utf-8")
    text = text.replace(
        "  fetch_db_dump: docker/dumps/app_db.custom\n",
        "  fetch_db_dump: docker/dumps/app_db.custom\n  fetch_metabase_db_dump: docker/dumps/metabase_db.custom\n",
    )
    text = text.replace(
        "      app:\n        db_name: app_db\n        via: ssh_native\n",
        "      app:\n        db_name: app_db\n        via: ssh_native\n"
        "      metabase:\n        db_name: metabase_db\n        via: ssh_native\n",
    )
    (tmp_path / "tooling.yaml").write_text(text, encoding="utf-8")
    cfg = load_project_config(tmp_path)
    _valid_dump(cfg.fetch_metabase_db_dump_path)
    order: list[str] = []

    monkeypatch.setattr(
        "catalpa_tooling.db_restore.ensure_db_service_running",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        "catalpa_tooling.db_restore.run_drop_create_metabase_database",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        "catalpa_tooling.db_restore.run_pg_restore",
        lambda *a, **k: (order.append("pgrestore") or 0),
    )
    monkeypatch.setattr(
        "catalpa_tooling.db_restore.run_post_metabase_db_restore_manage_commands",
        lambda *a, **k: (order.append("hooks") or 0),
    )

    rc = run_compose_metabase_dump_restore(
        cfg,
        compose_file="compose.yml",
        env_add={},
        env_name="dev",
    )
    assert rc == 0
    assert order == ["pgrestore", "hooks"]
