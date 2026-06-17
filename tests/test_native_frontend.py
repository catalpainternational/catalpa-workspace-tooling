"""Tests for ``native frontend`` / ``native vite`` handlers."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalpa_tooling.native_cli import _native_main, _nvm_use_shell_prefix, _run_frontend_dev


def test_run_frontend_dev_npm_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"name": "app"}', encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(
        f"""project:
  name: app
  root_marker: pyproject.toml
paths:
  backend: backend
  frontend: frontend
  scripts: scripts
  env_local: .env.local
  email_backend_dir: email
  fetch_db_dump: dump.custom
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
    credentials_optional_envs: []
stack:
  compose_project_default: app
  services:
    web: web
    proxy: proxy
    db: db
  images:
    registry_key: image_registry
    components:
      web: app-web
      proxy: app-proxy
      db: app-db
  healthcheck:
    service: web
    url: http://localhost:8000/
ops:
  install_prefix: /opt/app
  config_dir: /etc/app
  systemd_unit_prefix: app-
  transfer_workdir: .app-transfer
  default_db_container: app_db_1
  pgbackrest:
    postgres_conf: pg.conf
    pgbackrest_conf: pgbr.conf
    default_registry: ghcr.io/example
  restic:
    data_volume: media
  zabbix:
    unit_name: z.service
    userparams_file: z.conf
  systemd_units:
    pgbackrest: []
    restic: []
    timers_enable_pgbackrest: []
    timers_enable_restic: []
""",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='app'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    calls: list[tuple[list[str], Path]] = []

    def fake_run_cmd(cmd, *, cwd, env=None, check=False, **kwargs):
        calls.append((list(cmd), cwd))
        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("catalpa_tooling.native_cli.run_cmd", fake_run_cmd)
    rc = _run_frontend_dev()
    assert rc == 0
    assert len(calls) == 2
    assert calls[0][0] == ["npm", "install"]
    assert calls[1][0] == ["npm", "run", "dev"]
    assert calls[0][1] == frontend


def test_run_frontend_dev_yarn_start_with_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "bero"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        '{"name":"bero","packageManager":"yarn@4.9.2"}',
        encoding="utf-8",
    )
    (tmp_path / "tooling.yaml").write_text(
        f"""project:
  name: jid
  root_marker: pyproject.toml
paths:
  backend: .
  frontend: bero
  scripts: scripts
  env_local: .env.local
  email_backend_dir: media
  fetch_db_dump: dump.custom
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yaml
    dev_compose: compose.dev.yaml
    credentials_optional_envs: []
stack:
  compose_project_default: jid
  services:
    web: django
    proxy: caddy
    db: db
  images:
    registry_key: image_registry
    components:
      web: jid-django
      proxy: jid-caddy
      db: jid-db
  healthcheck:
    service: django
    url: http://localhost:8000/
ops:
  install_prefix: /opt/jid
  config_dir: /etc/jid
  systemd_unit_prefix: jid-
  transfer_workdir: .jid-transfer
  default_db_container: jid-db-1
  pgbackrest:
    postgres_conf: pg.conf
    pgbackrest_conf: pgbr.conf
    default_registry: ghcr.io/example
  restic:
    data_volume: media
  zabbix:
    unit_name: z.service
    userparams_file: z.conf
  systemd_units:
    pgbackrest: []
    restic: []
    timers_enable_pgbackrest: []
    timers_enable_restic: []
native:
  frontend:
    package_manager: yarn
    script: start
    node_version: "22"
    env:
      WEBPACK_DEVSERVER_PROXY_TARGET: http://127.0.0.1:8000
      SKIP_SW: "true"
""",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='jid'\n", encoding="utf-8")
    nvm_dir = tmp_path / ".nvm"
    nvm_dir.mkdir()
    (nvm_dir / "nvm.sh").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("catalpa_tooling.native_cli.Path.home", lambda: tmp_path)

    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run_cmd(cmd, *, cwd, env=None, check=False, **kwargs):
        calls.append((list(cmd), env))
        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("catalpa_tooling.native_cli.run_cmd", fake_run_cmd)
    rc = _run_frontend_dev()
    assert rc == 0
    assert calls[0][0][0] == "bash"
    assert "nvm use 22" in calls[0][0][2]
    assert "yarn install" in calls[0][0][2]
    assert calls[1][0][0] == "bash"
    assert "yarn run start" in calls[1][0][2]
    assert calls[1][1] is not None
    assert calls[1][1]["WEBPACK_DEVSERVER_PROXY_TARGET"] == "http://127.0.0.1:8000"
    assert calls[1][1]["SKIP_SW"] == "true"


def test_nvm_use_shell_prefix_prefers_nvmrc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nvm_dir = tmp_path / ".nvm"
    nvm_dir.mkdir()
    (nvm_dir / "nvm.sh").write_text("", encoding="utf-8")
    monkeypatch.setattr("catalpa_tooling.native_cli.Path.home", lambda: tmp_path)
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / ".nvmrc").write_text("20\n", encoding="utf-8")
    prefix = _nvm_use_shell_prefix(frontend, "22")
    assert prefix is not None
    assert "nvm use" in prefix
    assert "nvm use 22" not in prefix


def test_nvm_use_shell_prefix_uses_configured_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nvm_dir = tmp_path / ".nvm"
    nvm_dir.mkdir()
    (nvm_dir / "nvm.sh").write_text("", encoding="utf-8")
    monkeypatch.setattr("catalpa_tooling.native_cli.Path.home", lambda: tmp_path)
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    prefix = _nvm_use_shell_prefix(frontend, "22")
    assert prefix is not None
    assert "nvm use 22" in prefix


def test_run_frontend_dev_uses_nvm_when_node_version_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nvm_dir = tmp_path / ".nvm"
    nvm_dir.mkdir()
    (nvm_dir / "nvm.sh").write_text("", encoding="utf-8")
    monkeypatch.setattr("catalpa_tooling.native_cli.Path.home", lambda: tmp_path)

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(
        """project:
  name: app
  root_marker: pyproject.toml
paths:
  backend: backend
  frontend: frontend
  scripts: scripts
  env_local: .env.local
  email_backend_dir: email
  fetch_db_dump: dump.custom
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
    credentials_optional_envs: []
stack:
  compose_project_default: app
  services:
    web: web
    proxy: proxy
    db: db
  images:
    registry_key: image_registry
    components:
      web: app-web
      proxy: app-proxy
      db: app-db
  healthcheck:
    service: web
    url: http://localhost:8000/
ops:
  install_prefix: /opt/app
  config_dir: /etc/app
  systemd_unit_prefix: app-
  transfer_workdir: .app-transfer
  default_db_container: app_db_1
  pgbackrest:
    postgres_conf: pg.conf
    pgbackrest_conf: pgbr.conf
    default_registry: ghcr.io/example
  restic:
    data_volume: media
  zabbix:
    unit_name: z.service
    userparams_file: z.conf
  systemd_units:
    pgbackrest: []
    restic: []
    timers_enable_pgbackrest: []
    timers_enable_restic: []
native:
  frontend:
    node_version: "22"
""",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='app'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    bash_cmds: list[str] = []

    def fake_run_cmd(cmd, *, cwd, env=None, check=False, **kwargs):
        if cmd and cmd[0] == "bash" and len(cmd) >= 3:
            bash_cmds.append(cmd[2])
        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("catalpa_tooling.native_cli.run_cmd", fake_run_cmd)
    rc = _run_frontend_dev()
    assert rc == 0
    assert len(bash_cmds) == 2
    assert all("nvm use 22" in cmd for cmd in bash_cmds)


@pytest.mark.parametrize("command", ["frontend", "vite"])
def test_native_main_frontend_and_vite_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"name": "app"}', encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(
        """project:
  name: app
  root_marker: pyproject.toml
paths:
  backend: backend
  frontend: frontend
  scripts: scripts
  env_local: .env.local
  email_backend_dir: email
  fetch_db_dump: dump.custom
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
    credentials_optional_envs: []
stack:
  compose_project_default: app
  services:
    web: web
    proxy: proxy
    db: db
  images:
    registry_key: image_registry
    components:
      web: app-web
      proxy: app-proxy
      db: app-db
  healthcheck:
    service: web
    url: http://localhost:8000/
ops:
  install_prefix: /opt/app
  config_dir: /etc/app
  systemd_unit_prefix: app-
  transfer_workdir: .app-transfer
  default_db_container: app_db_1
  pgbackrest:
    postgres_conf: pg.conf
    pgbackrest_conf: pgbr.conf
    default_registry: ghcr.io/example
  restic:
    data_volume: media
  zabbix:
    unit_name: z.service
    userparams_file: z.conf
  systemd_units:
    pgbackrest: []
    restic: []
    timers_enable_pgbackrest: []
    timers_enable_restic: []
""",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='app'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["native", command])
    monkeypatch.setattr(
        "catalpa_tooling.native_cli._run_frontend_dev",
        lambda: (_ for _ in ()).throw(SystemExit(0)),
    )
    with pytest.raises(SystemExit) as exc:
        _native_main()
    assert exc.value.code == 0
