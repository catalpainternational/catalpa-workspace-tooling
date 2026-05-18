"""Routing tests for env-scoped dk zabbix integration."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import yaml

from catalpa_tooling import dk_cli
from catalpa_tooling import remote_deploy


def test_dry_run_does_not_short_circuit_zabbix() -> None:
    assert remote_deploy._dry_run_exits_before_compose_env(["zabbix", "install"]) is False


def test_zabbix_env_defaults_warns_when_tls_psk_in_info_yaml(capsys) -> None:
    remote_deploy._zabbix_env_defaults({"zbx_tlspsk": "secret"}, {})
    err = capsys.readouterr().err
    assert "credentials.yaml" in err
    assert "ZBX_TLSPSK" in err


def test_zabbix_env_defaults_merge_zbx_from_env_add() -> None:
    d = remote_deploy._zabbix_env_defaults(
        {"env": {"zbx_server_host": "z.example"}},
        {"ZBX_TLSPSK": "deadbeef", "COMPOSE_PROJECT_NAME": "pas_indmo"},
    )
    assert d["ZBX_SERVER_HOST"] == "z.example"
    assert d["ZBX_TLSPSK"] == "deadbeef"
    assert "COMPOSE_PROJECT_NAME" not in d


def test_zabbix_env_defaults_merges_top_level_zbx_metadata() -> None:
    d = remote_deploy._zabbix_env_defaults(
        {
            "name": "staging",
            "zbx_metadata": "kafemalirin",
            "env": {"compose_project_name": "pas_indmo"},
        },
        {},
    )
    assert d["ZBX_METADATA"] == "kafemalirin"
    assert d["COMPOSE_PROJECT_NAME"] not in d


def test_zabbix_env_defaults_env_overrides_top_level_zbx() -> None:
    d = remote_deploy._zabbix_env_defaults(
        {
            "zbx_metadata": "top",
            "env": {"zbx_metadata": "from-env"},
        },
        {},
    )
    assert d["ZBX_METADATA"] == "from-env"


def test_cmd_deploy_routes_zabbix_with_env_defaults_and_ssh_target(
    tmp_path, monkeypatch, isolated_tooling: None
) -> None:
    env_name = "staging"
    deploy_dir = tmp_path / "docker" / "envs" / env_name
    deploy_dir.mkdir(parents=True)
    info = {
        "name": env_name,
        "docker_host": "ssh://deploy@example.test",
        "env": {
            "zbx_server_host": "zabbix.example.test",
            "zbx_active_allow": True,
        },
    }
    (deploy_dir / "info.yaml").write_text(yaml.safe_dump(info), encoding="utf-8")

    from catalpa_tooling.config import load_project_config
    from tests.helpers import write_minimal_tooling_tree

    write_minimal_tooling_tree(tmp_path)
    config = load_project_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(remote_deploy, "resolve_compose_file_from_info", lambda *_: "compose.yml")
    monkeypatch.setattr(
        remote_deploy,
        "load_managed_deploy_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            env_add={},
            docker_host="ssh://deploy@example.test",
            site_origin="https://example.test",
            use_prepulled_registry=False,
        ),
    )
    monkeypatch.setattr(
        remote_deploy,
        "resolve_env_with_compose_project",
        lambda _compose_file, env_add, **_kwargs: env_add,
    )

    called: dict[str, object] = {}

    def fake_run_zabbix(argv, **kwargs):
        called["argv"] = argv
        called.update(kwargs)
        return 23

    monkeypatch.setattr(remote_deploy, "run_zabbix_deploy", fake_run_zabbix)

    ns = argparse.Namespace(
        env_name=env_name,
        compose_args=["zabbix", "install", "--hostname", "stage-web-01"],
        dry_run=False,
        yes=False,
        tag=None,
    )
    rc = remote_deploy._cmd_deploy(ns, config)
    assert rc == 23
    assert called["argv"] == ["install", "--hostname", "stage-web-01"]
    assert called["prog"] == f"dk {env_name} zabbix"
    assert called["ssh_target"] == "deploy@example.test"
    assert called["site_origin"] == "https://example.test"
    assert called["env_defaults"]["ZBX_SERVER_HOST"] == "zabbix.example.test"
    assert called["env_defaults"]["ZBX_ACTIVE_ALLOW"] == "True"


def test_cmd_deploy_routes_zabbix_with_local_fallback(
    tmp_path, monkeypatch, isolated_tooling: None
) -> None:
    env_name = "local"
    deploy_dir = tmp_path / "docker" / "envs" / env_name
    deploy_dir.mkdir(parents=True)
    info = {
        "name": env_name,
        "docker_host": "unix:///var/run/docker.sock",
        "env": {},
    }
    (deploy_dir / "info.yaml").write_text(yaml.safe_dump(info), encoding="utf-8")

    from catalpa_tooling.config import load_project_config
    from tests.helpers import write_minimal_tooling_tree

    write_minimal_tooling_tree(tmp_path)
    config = load_project_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(remote_deploy, "resolve_compose_file_from_info", lambda *_: "compose.yml")
    monkeypatch.setattr(
        remote_deploy,
        "load_managed_deploy_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            env_add={},
            docker_host="unix:///var/run/docker.sock",
            site_origin="http://localhost:5173",
            use_prepulled_registry=False,
        ),
    )
    monkeypatch.setattr(
        remote_deploy,
        "resolve_env_with_compose_project",
        lambda _compose_file, env_add, **_kwargs: env_add,
    )

    called: dict[str, object] = {}

    def fake_run_zabbix(argv, **kwargs):
        called["argv"] = argv
        called.update(kwargs)
        return 11

    monkeypatch.setattr(remote_deploy, "run_zabbix_deploy", fake_run_zabbix)

    ns = argparse.Namespace(
        env_name=env_name,
        compose_args=["zabbix", "logs", "-n", "20"],
        dry_run=False,
        yes=False,
        tag=None,
    )
    rc = remote_deploy._cmd_deploy(ns, config)
    assert rc == 11
    assert called["argv"] == ["logs", "-n", "20"]
    assert called["ssh_target"] is None
    assert called["site_origin"] == "http://localhost:5173"


def test_top_level_dk_zabbix_dispatches(monkeypatch, minimal_config) -> None:
    called: list[list[str]] = []

    def fake_zabbix(argv: list[str], config) -> int:
        called.append(argv)
        return 0

    monkeypatch.setattr(dk_cli.ProjectConfig, "from_cwd", lambda: minimal_config)
    monkeypatch.setattr(dk_cli, "_cmd_zabbix", fake_zabbix)
    monkeypatch.setattr(dk_cli.sys, "argv", ["dk", "zabbix", "logs", "-n", "5"])

    try:
        dk_cli._main_impl()
    except SystemExit as exc:
        assert exc.code == 0
    assert called == [["logs", "-n", "5"]]
