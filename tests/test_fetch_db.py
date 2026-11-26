"""Tests for config-driven fetch_db and unified restore."""

from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling import stack_commands
from catalpa_tooling.compose_backups import pgbackrest_restore_configured
from catalpa_tooling.db_archive import restore_archive
from catalpa_tooling.fetch_db import dump_path_usable, run_fetch_all_dbs
from catalpa_tooling.pgbackrest_volume_config import PREFIX_READ
from catalpa_tooling.stack_backend import ComposeBackend
from tests.helpers import spy_compose_backend


def _backend(cfg, env_add=None) -> ComposeBackend:
    b = ComposeBackend.with_env(ComposeBackend.from_parts(cfg, "dev", "compose.yml", dict(env_add or {})).ctx, dict(env_add or {}))
    return b


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


def test_run_fetch_via_dk_dumps_each_entrys_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_tooling: None
) -> None:
    """#173: a ``via: dk`` metabase entry dumps its own ``db_name``, not ``$APP_DB``."""
    _write_tooling(tmp_path)
    tooling = tmp_path / "tooling.yaml"
    text = tooling.read_text(encoding="utf-8")
    text = text.replace(
        "  fetch_db_dump: docker/dumps/app_db.custom\n",
        "  fetch_db_dump: docker/dumps/app_db.custom\n"
        "  fetch_metabase_db_dump: docker/dumps/metabase_db.custom\n",
    ).replace(
        "      app:\n        db_name: app_db\n        via: ssh_native\n",
        "      app:\n        db_name: app_db\n        via: dk\n"
        "      metabase:\n        db_name: metabase_db\n        via: dk\n",
    )
    tooling.write_text(text, encoding="utf-8")
    cfg = load_project_config(tmp_path)

    class _Ctx:
        compose_file = "compose.yml"
        env_add: dict[str, str] = {}
        config = cfg
        env_name = "prod"
        docker_host = ""

    scripts: list[str] = []

    class _Result:
        returncode = 0
        stderr = b""

    def fake_run(cmd: list[str], **kwargs: object) -> _Result:
        scripts.append(cmd[-1])
        kwargs["stdout"].write(b"PGDMP" + b"x" * 200_000)  # type: ignore[attr-defined]
        return _Result()

    monkeypatch.setattr("catalpa_tooling.managed_deploy_env.load_managed_deploy_context", lambda *_a: _Ctx())
    monkeypatch.setattr("catalpa_tooling.pgbackrest_db.db_service_responds", lambda *_a: True)
    monkeypatch.setattr("catalpa_tooling.pgbackrest_db.run_cmd", fake_run)

    run_fetch_all_dbs(cfg)

    assert len(scripts) == 2
    app_script, metabase_script = scripts
    assert '-d "$APP_DB" -Fc' in app_script
    assert "-d metabase_db -Fc" in metabase_script
    assert '"$APP_DB" -Fc' not in metabase_script
    assert dump_path_usable(cfg.fetch_metabase_db_dump_path)


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
    rc = stack_commands.db_restore(_backend(cfg), dumps=True, yes=True)
    assert rc == 1


def test_unified_restore_auto_fetch_then_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_tooling: None,
) -> None:
    _write_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    _valid_dump(cfg.fetch_db_dump_path)

    calls = spy_compose_backend(monkeypatch)

    rc = stack_commands.db_restore(_backend(cfg), yes=True)
    assert rc == 0
    assert calls == ["ensure", ("copy", cfg.fetch_db_dump_path), "drop_create", "pg_restore", "rm", "hooks:app"]


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

    monkeypatch.setattr("catalpa_tooling.fetch_db.run_fetch_all_dbs", _fake_fetch)
    spy_compose_backend(monkeypatch)

    rc = stack_commands.db_restore(_backend(cfg), yes=True)
    assert rc == 0
    assert called == ["fetch"]


def _write_tooling_with_metabase(tmp_path: Path) -> None:
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


def test_metabase_dump_restore_runs_hooks_after_pgrestore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_tooling: None,
) -> None:
    _write_tooling_with_metabase(tmp_path)
    cfg = load_project_config(tmp_path)
    _valid_dump(cfg.fetch_metabase_db_dump_path)
    calls = spy_compose_backend(monkeypatch)

    rc = restore_archive(_backend(cfg), cfg.fetch_metabase_db_dump_path, target="metabase")
    assert rc == 0
    assert calls == ["ensure", ("copy", cfg.fetch_metabase_db_dump_path), "drop_create", "pg_restore", "rm", "hooks:metabase"]


def test_restore_refuses_before_drop_when_metabase_dump_is_bad(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_tooling: None,
) -> None:
    """A bad Metabase dump must not cost the app DB: both are checked before any drop (#124)."""
    _write_tooling_with_metabase(tmp_path)
    cfg = load_project_config(tmp_path)
    _valid_dump(cfg.fetch_db_dump_path)
    cfg.fetch_metabase_db_dump_path.write_bytes(b"-- PostgreSQL database dump\n")
    calls = spy_compose_backend(monkeypatch)

    rc = stack_commands.db_restore(_backend(cfg), yes=True)
    assert rc == 1
    assert calls == []
