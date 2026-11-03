"""Tests for host-wide Docker log rotation (#109)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from catalpa_tooling.cli.dk_argv import normalize_dk_env_argv
from catalpa_tooling.cloud_config.render import render_droplet_bootstrap
from catalpa_tooling.config import load_project_config
from catalpa_tooling.dc_backup.ssh_install import RemoteResult
from catalpa_tooling.dk_parser import build_dk_parser
from catalpa_tooling.docker_logging import (
    DAEMON_LOG_CONFIG,
    HostLogStatus,
    ContainerLogConfig,
    daemon_config_matches,
    merge_daemon_config,
    parse_container_log_configs,
    parse_daemon_json,
    probe_host_logging,
    report_host_logging,
)
from tests.helpers import write_minimal_tooling_tree


def test_bootstrap_writes_daemon_json_matching_module() -> None:
    cfg = yaml.safe_load(render_droplet_bootstrap())
    entries = {f["path"]: f for f in cfg["write_files"]}
    daemon = entries["/etc/docker/daemon.json"]
    assert json.loads(daemon["content"]) == DAEMON_LOG_CONFIG
    assert daemon_config_matches(json.loads(daemon["content"]))


def test_merge_keeps_unrelated_keys_and_replaces_log_opts() -> None:
    merged = merge_daemon_config(
        {"log-driver": "json-file", "log-opts": {"labels": "x"}, "live-restore": True}
    )
    assert merged["live-restore"] is True
    assert merged["log-driver"] == "local"
    assert merged["log-opts"] == {"max-size": "20m", "max-file": "5"}


def test_merge_from_missing_file() -> None:
    assert merge_daemon_config(None) == DAEMON_LOG_CONFIG


def test_daemon_config_matches() -> None:
    assert not daemon_config_matches(None)
    assert not daemon_config_matches({"log-driver": "json-file"})
    assert not daemon_config_matches({"log-driver": "local", "log-opts": {"max-size": "1g"}})
    assert daemon_config_matches(DAEMON_LOG_CONFIG)


def test_parse_daemon_json() -> None:
    assert parse_daemon_json("") is None
    assert parse_daemon_json('{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        parse_daemon_json("[]")
    with pytest.raises(ValueError):
        parse_daemon_json("{not json")


def test_parse_container_log_configs_flags_uncapped_json_file() -> None:
    text = (
        '/proj-db-1\tjson-file\t{}\n'
        '/proj-web-1\tjson-file\t{"max-file":"3","max-size":"10m"}\n'
        '/proj-caddy-1\tlocal\t{}\n'
        '/proj-redis-1\tjournald\tnull\n'
    )
    containers = parse_container_log_configs(text)
    assert [c.name for c in containers] == ["proj-db-1", "proj-web-1", "proj-caddy-1", "proj-redis-1"]
    assert [c.name for c in containers if c.uncapped] == ["proj-db-1"]


def _fake_remote(responses: dict[str, RemoteResult]):
    def fake(ssh_target: str, remote_cmd: str, *, dry_run: bool = False) -> RemoteResult:
        for prefix, result in responses.items():
            if remote_cmd.startswith(prefix):
                return result
        raise AssertionError(f"unexpected remote command {remote_cmd!r}")

    return fake


def test_probe_host_logging_default_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.dc_backup.ssh_install.remote_run_capture",
        _fake_remote(
            {
                "docker info": RemoteResult(0, "json-file\n", ""),
                "if [ -f": RemoteResult(0, "", ""),
                "docker ps": RemoteResult(0, "/proj-db-1\tjson-file\t{}\n", ""),
            }
        ),
    )
    status = probe_host_logging("root@host")
    assert status is not None
    assert status.driver == "json-file"
    assert status.daemon_json is None
    assert not status.daemon_ok
    assert [c.name for c in status.uncapped] == ["proj-db-1"]


def test_report_host_logging_ok(capsys: pytest.CaptureFixture[str]) -> None:
    status = HostLogStatus("local", dict(DAEMON_LOG_CONFIG), [ContainerLogConfig("db", "local", {})])
    assert report_host_logging("app", status, env_name="prod") is True
    assert "warning" not in capsys.readouterr().err


def test_report_host_logging_points_at_fixes(capsys: pytest.CaptureFixture[str]) -> None:
    status = HostLogStatus("json-file", None, [ContainerLogConfig("db", "json-file", {})])
    assert report_host_logging("app", status, env_name="prod") is False
    err = capsys.readouterr().err
    assert "dk prod host logging --apply" in err
    assert "dk prod up -d --force-recreate" in err
    assert "db" in err


def test_dk_parser_host_logging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_minimal_tooling_tree(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "staging"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text("name: staging\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    parser = build_dk_parser(load_project_config(tmp_path))
    ns = parser.parse_args(normalize_dk_env_argv(["staging", "host", "logging", "--apply"]))
    assert ns.env_command == "host"
    assert ns.host_command == "logging"
    assert ns.apply is True
    assert ns.target == "app"
