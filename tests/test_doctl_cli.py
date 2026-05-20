"""Routing tests for ``dk digoc`` (doctl_cli.run_digoc)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from catalpa_tooling import doctl_cli


def test_auth_init_adds_interactive_on_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    monkeypatch.setattr(doctl_cli.sys.stdin, "isatty", lambda: True)
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(doctl_cli, "run_doctl", mock_run)
    assert doctl_cli._cmd_auth_init([]) == 0
    mock_run.assert_called_once_with(["auth", "init", "--interactive"])


def test_auth_init_passes_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    monkeypatch.setattr(doctl_cli.sys.stdin, "isatty", lambda: False)
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(doctl_cli, "run_doctl", mock_run)
    assert doctl_cli._cmd_auth_init(["-t", "secret"]) == 0
    mock_run.assert_called_once_with(["auth", "init", "--access-token", "secret"])


def test_auth_init_non_tty_without_token_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    monkeypatch.setattr(doctl_cli.sys.stdin, "isatty", lambda: False)
    assert doctl_cli._cmd_auth_init([]) == 1


def test_auth_remove_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(doctl_cli, "run_doctl", mock_run)
    assert doctl_cli._cmd_auth_forward("remove", ["--context", "default"]) == 0
    mock_run.assert_called_once_with(["auth", "remove", "--context", "default"])


def test_auth_list_invokes_doctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(doctl_cli, "run_doctl", mock_run)
    assert doctl_cli._cmd_auth_list([]) == 0
    mock_run.assert_called_once_with(["auth", "list"], context=None)


def test_projects_list_invokes_doctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(doctl_cli, "run_doctl", mock_run)
    assert doctl_cli._cmd_projects_list([]) == 0
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0:2] == ["projects", "list"]


def test_droplets_list_with_project_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    monkeypatch.setattr(
        doctl_cli,
        "resolve_project_id",
        lambda project, do_config=None, context=None: "proj-1",
    )
    listed: list[str] = []

    def fake_list(project_id: str, *, context, columns, as_json, config=None) -> int:
        listed.append(project_id)
        return 0

    monkeypatch.setattr(doctl_cli, "list_project_droplets", fake_list)
    monkeypatch.setattr(
        doctl_cli.ProjectConfig,
        "from_cwd",
        staticmethod(
            lambda: (_ for _ in ()).throw(doctl_cli.ProjectConfigError("no manifest"))
        ),
    )
    assert doctl_cli._cmd_droplets_list(["--project", "my-proj"]) == 0
    assert listed == ["proj-1"]


def test_cloud_config_print(capsys: pytest.CaptureFixture) -> None:
    assert doctl_cli._cmd_cloud_config_print([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("#cloud-config")
    assert "timezone: Asia/Dili" in out


def test_droplets_create_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        doctl_cli,
        "resolve_project_id",
        lambda project, do_config=None, context=None: "proj-1",
    )
    monkeypatch.setattr(
        doctl_cli,
        "_load_do_config_for_droplets",
        lambda project_flag: (None, None),
    )
    created: list[str] = []

    def fake_create(name, **kwargs):
        created.append(name)
        assert kwargs["dry_run"] is True
        return 0

    monkeypatch.setattr(doctl_cli, "create_droplet", fake_create)
    assert (
        doctl_cli._cmd_droplets_create(
            [
                "my-host",
                "--project",
                "p",
                "--size",
                "s-1vcpu-1gb",
                "--region",
                "sgp1",
                "--ssh-key",
                "key-1",
                "--dry-run",
            ]
        )
        == 0
    )
    assert created == ["my-host"]


def test_droplets_create_for_env_default_name_delegates(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("description: prod\n", encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(
        "project:\n  name: catalpa-site\n  root_marker: tooling.yaml\n"
        "paths:\n  backend: b\n  frontend: b\n  scripts: s\n  env_local: e\n"
        "  email_backend_dir: m\n  fetch_db_dump: d\n"
        "  deploy:\n    envs_dir: docker/envs\n    images_config: i.yaml\n"
        "    default_compose: c.yaml\n    dev_compose: d.yaml\n"
        "    credentials_optional_envs: []\n"
        "stack:\n  compose_project_default: x\n  services:\n    web: w\n    proxy: p\n    db: d\n"
        "  images:\n    registry_key: r\n    components:\n      web: w\n      proxy: p\n      db: d\n"
        "  healthcheck:\n    service: w\n    url: http://localhost/\n"
        "ops:\n  install_prefix: /opt\n  config_dir: /etc\n  systemd_unit_prefix: x-\n"
        "  transfer_workdir: .t\n  default_db_container: db\n"
        "  pgbackrest:\n    postgres_conf: a\n    pgbackrest_conf: b\n"
        "    default_registry: ghcr.io/x\n    restore_temp_prefix: r_\n"
        "  zabbix:\n    unit_name: z.service\n    userparams_file: z.conf\n"
        "  systemd_units:\n    pgbackrest: []\n    restic: []\n"
        "    timers_enable_pgbackrest: []\n    timers_enable_restic: []\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    delegated: list[str] = []

    def fake_host_create(cfg, env, argv, **kwargs):
        delegated.append(env)
        return 0

    monkeypatch.setattr(doctl_cli, "cmd_env_host_create", fake_host_create)
    assert doctl_cli._cmd_droplets_create(
        ["--for-env", "prod", "--size", "s-1vcpu-1gb", "--region", "sgp1", "--dry-run"]
    ) == 0
    assert delegated == ["prod"]


def test_droplets_create_for_env_delegates_to_host_create(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        "digitalocean:\n  droplet_name: marktwain-prod\n",
        encoding="utf-8",
    )
    (tmp_path / "tooling.yaml").write_text(
        "project:\n  name: t\n  root_marker: tooling.yaml\n"
        "paths:\n  backend: b\n  frontend: b\n  scripts: s\n  env_local: e\n"
        "  email_backend_dir: m\n  fetch_db_dump: d\n"
        "  deploy:\n    envs_dir: docker/envs\n    images_config: i.yaml\n"
        "    default_compose: c.yaml\n    dev_compose: d.yaml\n"
        "    credentials_optional_envs: []\n"
        "stack:\n  compose_project_default: x\n  services:\n    web: w\n    proxy: p\n    db: d\n"
        "  images:\n    registry_key: r\n    components:\n      web: w\n      proxy: p\n      db: d\n"
        "  healthcheck:\n    service: w\n    url: http://localhost/\n"
        "ops:\n  install_prefix: /opt\n  config_dir: /etc\n  systemd_unit_prefix: x-\n"
        "  transfer_workdir: .t\n  default_db_container: db\n"
        "  pgbackrest:\n    postgres_conf: a\n    pgbackrest_conf: b\n"
        "    default_registry: ghcr.io/x\n    restore_temp_prefix: r_\n"
        "  zabbix:\n    unit_name: z.service\n    userparams_file: z.conf\n"
        "  systemd_units:\n    pgbackrest: []\n    restic: []\n"
        "    timers_enable_pgbackrest: []\n    timers_enable_restic: []\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    delegated: list[tuple] = []

    def fake_host_create(cfg, env, argv, **kwargs):
        delegated.append((env, argv, kwargs.get("deprecation_message")))
        return 0

    monkeypatch.setattr(doctl_cli, "cmd_env_host_create", fake_host_create)
    assert doctl_cli._cmd_droplets_create(
        ["--for-env", "prod", "--size", "s-1vcpu-1gb", "--region", "sgp1", "--dry-run"]
    ) == 0
    assert delegated[0][0] == "prod"
    assert "--size" in delegated[0][1]
    assert "Deprecated" in (delegated[0][2] or "")


def test_droplets_create_for_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        "digitalocean:\n  droplet_name: marktwain-prod\n",
        encoding="utf-8",
    )
    (tmp_path / "tooling.yaml").write_text(
        "project:\n  name: t\n  root_marker: tooling.yaml\n"
        "paths:\n  backend: b\n  frontend: b\n  scripts: s\n  env_local: e\n"
        "  email_backend_dir: m\n  fetch_db_dump: d\n"
        "  deploy:\n    envs_dir: docker/envs\n    images_config: i.yaml\n"
        "    default_compose: c.yaml\n    dev_compose: d.yaml\n"
        "    credentials_optional_envs: []\n"
        "stack:\n  compose_project_default: x\n  services:\n    web: w\n    proxy: p\n    db: d\n"
        "  images:\n    registry_key: r\n    components:\n      web: w\n      proxy: p\n      db: d\n"
        "  healthcheck:\n    service: w\n    url: http://localhost/\n"
        "ops:\n  install_prefix: /opt\n  config_dir: /etc\n  systemd_unit_prefix: x-\n"
        "  transfer_workdir: .t\n  default_db_container: db\n"
        "  pgbackrest:\n    postgres_conf: a\n    pgbackrest_conf: b\n"
        "    default_registry: ghcr.io/x\n    restore_temp_prefix: r_\n"
        "  zabbix:\n    unit_name: z.service\n    userparams_file: z.conf\n"
        "  systemd_units:\n    pgbackrest: []\n    restic: []\n"
        "    timers_enable_pgbackrest: []\n    timers_enable_restic: []\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctl_cli, "ensure_doctl_available", lambda: None)
    monkeypatch.setattr(
        doctl_cli,
        "resolve_project_id",
        lambda project, do_config=None, context=None: "proj-1",
    )
    monkeypatch.setattr(
        doctl_cli,
        "_load_do_config_for_droplets",
        lambda project_flag: (None, None),
    )
    created: list[tuple[str, dict]] = []

    def fake_create(name, **kwargs):
        created.append((name, kwargs))
        return 0

    monkeypatch.setattr(doctl_cli, "create_droplet", fake_create)
    assert doctl_cli._cmd_droplets_create(
        [
            "marktwain-prod",
            "--size",
            "s-1vcpu-1gb",
            "--region",
            "sgp1",
            "--dry-run",
        ]
    ) == 0
    assert created[0][0] == "marktwain-prod"
    assert created[0][1]["for_env"] is None


def test_run_digoc_routes_auth_init(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctl_cli, "_cmd_auth_init", lambda argv: 42)
    assert doctl_cli.run_digoc(["auth", "init"]) == 42
