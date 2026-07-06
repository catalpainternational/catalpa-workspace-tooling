"""Tests for catalpa_tooling.zabbix_systemd."""

from catalpa_tooling import zabbix_systemd as zs


def test_merge_env_keys_serializes_expected_order() -> None:
    body, reason = zs._merge_env_keys(
        None,
        server="zabbix.example.test",
        hostname="host-a",
        active_allow=False,
    )
    assert reason == "created"
    assert body is not None
    assert "ZBX_SERVER_HOST=zabbix.example.test" in body
    assert "ZBX_HOSTNAME=host-a" in body
    assert "ZBX_ACTIVE_ALLOW=false" in body


def test_merge_env_keys_merges_extra_zbx_keys_from_info_yaml() -> None:
    body, reason = zs._merge_env_keys(
        None,
        server="srv.example",
        hostname="h1",
        active_allow=True,
        env_defaults={"ZBX_METADATA": "role=web", "ZBX_METADATAITEM": "system.uname"},
    )
    assert reason == "created"
    assert body is not None
    assert "ZBX_METADATA=role=web" in body
    assert "ZBX_METADATAITEM=system.uname" in body


def test_merge_env_keys_sets_tls_connect_psk_when_psk_pair_present() -> None:
    body, reason = zs._merge_env_keys(
        None,
        server="srv.example",
        hostname="h1",
        active_allow=True,
        env_defaults={
            "ZBX_TLSPSK": "deadbeef",
            "ZBX_TLSPSKIDENTITY": "agent-1",
        },
    )
    assert reason == "created"
    assert body is not None
    assert "ZBX_TLSCONNECT=psk" in body


def test_merge_env_keys_does_not_override_explicit_tls_connect() -> None:
    body, _ = zs._merge_env_keys(
        None,
        server="srv.example",
        hostname="h1",
        active_allow=True,
        env_defaults={
            "ZBX_TLSPSK": "deadbeef",
            "ZBX_TLSPSKIDENTITY": "agent-1",
            "ZBX_TLSCONNECT": "cert",
        },
    )
    assert "ZBX_TLSCONNECT=cert" in body


def test_merge_env_keys_core_triple_wins_over_reserved_yaml_keys_in_extras() -> None:
    """Reserved ``ZBX_*`` keys are applied via install options, not the extras merge."""
    body, _ = zs._merge_env_keys(
        None,
        server="from-core",
        hostname="host-core",
        active_allow=False,
        env_defaults={
            "ZBX_SERVER_HOST": "yaml-only-server",
            "ZBX_METADATA": "x",
        },
    )
    assert "ZBX_SERVER_HOST=from-core" in body
    assert "yaml-only-server" not in body
    assert "ZBX_METADATA=x" in body


def test_merge_env_keys_updates_existing_with_only_extras() -> None:
    existing = (
        "ZBX_SERVER_HOST=srv\n"
        "ZBX_HOSTNAME=h\n"
        "ZBX_ACTIVE_ALLOW=true\n"
    )
    body, reason = zs._merge_env_keys(
        existing,
        server=None,
        hostname=None,
        active_allow=None,
        env_defaults={"ZBX_METADATA": "k=v"},
    )
    assert reason == "updated"
    assert body is not None
    assert "ZBX_SERVER_HOST=srv" in body
    assert "ZBX_METADATA=k=v" in body


def test_ensure_env_file_dry_run_prints_merged_env_contents(monkeypatch, capsys) -> None:
    monkeypatch.setattr(zs, "_read_env_file", lambda: None)
    zs._ensure_env_file(
        server="z.example",
        hostname="host1",
        active_allow=False,
        dry_run=True,
        env_defaults=None,
    )
    out = capsys.readouterr().out
    assert "[dry-run] env file contents:" in out
    assert "ZBX_SERVER_HOST=z.example" in out
    assert "ZBX_HOSTNAME=host1" in out
    assert "ZBX_ACTIVE_ALLOW=false" in out


def test_ensure_env_file_dry_run_unchanged_shows_existing_file(monkeypatch, capsys) -> None:
    existing = "ZBX_SERVER_HOST=old\nZBX_HOSTNAME=h\nZBX_ACTIVE_ALLOW=true\n"
    monkeypatch.setattr(zs, "_read_env_file", lambda: existing)
    zs._ensure_env_file(
        server=None,
        hostname=None,
        active_allow=None,
        dry_run=True,
        env_defaults=None,
    )
    out = capsys.readouterr().out
    assert "unchanged (no write)" in out
    assert "ZBX_SERVER_HOST=old" in out


def test_merge_env_keys_unchanged_when_no_overrides_and_no_extras() -> None:
    existing = (
        "ZBX_SERVER_HOST=srv\n"
        "ZBX_HOSTNAME=h\n"
        "ZBX_ACTIVE_ALLOW=true\n"
    )
    body, reason = zs._merge_env_keys(
        existing,
        server=None,
        hostname=None,
        active_allow=None,
        env_defaults=None,
    )
    assert reason == "unchanged"
    assert body is None


def test_install_options_cli_overrides_env_defaults() -> None:
    server, host, active = zs._install_options_from_env_and_cli(
        args_server="cli-server",
        args_hostname="cli-host",
        args_active_allow=True,
        env_defaults={
            "ZBX_SERVER_HOST": "yaml-server",
            "ZBX_HOSTNAME": "yaml-host",
            "ZBX_ACTIVE_ALLOW": "false",
        },
        site_origin=None,
    )
    assert server == "cli-server"
    assert host == "cli-host"
    assert active is True


def test_install_options_falls_back_to_env_defaults() -> None:
    server, host, active = zs._install_options_from_env_and_cli(
        args_server=None,
        args_hostname=None,
        args_active_allow=None,
        env_defaults={
            "ZBX_SERVER_HOST": "yaml-server",
            "ZBX_HOSTNAME": "yaml-host",
            "ZBX_ACTIVE_ALLOW": "yes",
        },
        site_origin=None,
    )
    assert server == "yaml-server"
    assert host == "yaml-host"
    assert active is True


def test_hostname_from_site_origin() -> None:
    assert zs.hostname_from_site_origin("https://demo.example.com/path") == "demo.example.com"
    assert zs.hostname_from_site_origin("http://localhost:5173") == "localhost"
    assert zs.hostname_from_site_origin("") is None


def test_install_options_hostname_from_site_origin() -> None:
    server, host, active = zs._install_options_from_env_and_cli(
        args_server=None,
        args_hostname=None,
        args_active_allow=None,
        env_defaults={},
        site_origin="https://app.prod.example.org",
    )
    assert server is None
    assert host == "app.prod.example.org"
    assert active is True


def test_install_options_active_allow_defaults_true_without_yaml_key() -> None:
    _, _, active = zs._install_options_from_env_and_cli(
        args_server=None,
        args_hostname=None,
        args_active_allow=None,
        env_defaults={},
        site_origin=None,
    )
    assert active is True


def test_install_options_yaml_can_disable_active_checks() -> None:
    _, _, active = zs._install_options_from_env_and_cli(
        args_server=None,
        args_hostname=None,
        args_active_allow=None,
        env_defaults={"ZBX_ACTIVE_ALLOW": "false"},
        site_origin="https://x.example.com",
    )
    assert active is False


def test_run_zabbix_deploy_routes_to_remote_restart(monkeypatch, minimal_config) -> None:
    called: dict[str, object] = {}

    def fake_restart_remote(ssh_target: str, **kwargs):
        called["ssh_target"] = ssh_target
        called.update(kwargs)
        return 5

    monkeypatch.setattr(zs, "cmd_restart_remote", fake_restart_remote)
    rc = zs.run_zabbix_deploy(
        ["restart"],
        config=minimal_config,
        prog="dk staging zabbix",
        ssh_target="deploy@example.test",
        dry_run=False,
        env_defaults=None,
        site_origin=None,
    )
    assert rc == 5
    assert called["ssh_target"] == "deploy@example.test"
    assert called["dry_run"] is False


def test_run_zabbix_deploy_routes_to_local_restart(monkeypatch, minimal_config) -> None:
    called: dict[str, object] = {}

    def fake_restart(**kwargs):
        called.update(kwargs)
        return 6

    monkeypatch.setattr(zs, "cmd_restart", fake_restart)
    rc = zs.run_zabbix_deploy(
        ["restart", "--dry-run"],
        config=minimal_config,
        prog="dk local zabbix",
        ssh_target=None,
        dry_run=False,
        env_defaults=None,
        site_origin=None,
    )
    assert rc == 6
    assert called["dry_run"] is True


def test_run_zabbix_deploy_routes_to_remote_install(monkeypatch, minimal_config) -> None:
    called: dict[str, object] = {}

    def fake_install_remote(ssh_target: str, **kwargs):
        called["ssh_target"] = ssh_target
        called.update(kwargs)
        return 7

    monkeypatch.setattr(zs, "cmd_install_remote", fake_install_remote)
    rc = zs.run_zabbix_deploy(
        ["install", "--server", "srv", "--dry-run"],
        config=minimal_config,
        prog="dk staging zabbix",
        ssh_target="deploy@example.test",
        dry_run=False,
        env_defaults=None,
        site_origin=None,
    )
    assert rc == 7
    assert called["ssh_target"] == "deploy@example.test"
    assert called["server"] == "srv"
    assert called["dry_run"] is True


def test_run_zabbix_deploy_routes_to_local_install_and_ors_dry_run(
    monkeypatch, minimal_config
) -> None:
    called: dict[str, object] = {}

    def fake_install(**kwargs):
        called.update(kwargs)
        return 9

    monkeypatch.setattr(zs, "cmd_install", fake_install)
    rc = zs.run_zabbix_deploy(
        ["install", "--server", "srv"],
        config=minimal_config,
        prog="dk local zabbix",
        ssh_target=None,
        dry_run=True,
        env_defaults=None,
        site_origin=None,
    )
    assert rc == 9
    assert called["server"] == "srv"
    assert called["dry_run"] is True
    assert called["env_defaults"] is None


def test_run_zabbix_deploy_passes_env_defaults_to_local_install(
    monkeypatch, minimal_config
) -> None:
    called: dict[str, object] = {}

    def fake_install(**kwargs):
        called.update(kwargs)
        return 0

    monkeypatch.setattr(zs, "cmd_install", fake_install)
    extras = {"ZBX_METADATA": "env=staging"}
    rc = zs.run_zabbix_deploy(
        ["install"],
        config=minimal_config,
        prog="dk staging zabbix",
        ssh_target=None,
        dry_run=False,
        env_defaults=extras,
        site_origin=None,
    )
    assert rc == 0
    assert called["env_defaults"] == extras


def test_run_zabbix_deploy_install_rejects_without_zbx_or_cli(
    capsys, minimal_config
) -> None:
    rc = zs.run_zabbix_deploy(
        ["install"],
        config=minimal_config,
        prog="dk staging zabbix",
        ssh_target=None,
        dry_run=False,
        env_defaults={},
        site_origin="https://app.example.com",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "no Zabbix configuration" in err


def test_render_userparams_conf_v2_container_name() -> None:
    body = zs.render_userparams_conf(
        {"COMPOSE_PROJECT_NAME": "pas_indmo", "ZABBIX_USERPARAMETER_RESTIC": "false"}
    )
    assert "UserParameter=pgbackrest.info," in body
    assert "pas_indmo-db-1" in body
    assert "restic.snapshots" not in body


def test_render_userparams_conf_legacy_naming() -> None:
    body = zs.render_userparams_conf(
        {
            "COMPOSE_PROJECT_NAME": "pas_indmo",
            "ZABBIX_COMPOSE_CONTAINER_NAMING": "legacy",
            "ZABBIX_USERPARAMETER_RESTIC": "false",
        }
    )
    assert "pas_indmo_db_1" in body


def test_render_userparams_conf_all_disabled() -> None:
    body = zs.render_userparams_conf(
        {
            "ZABBIX_USERPARAMETER_PGBACKREST": "false",
            "ZABBIX_USERPARAMETER_RESTIC": "false",
        }
    )
    assert "UserParameter=" not in body


def test_unit_file_includes_userparams_mount(minimal_config) -> None:
    zs._apply_config_globals(minimal_config)
    unit = zs._unit_file_content(image=zs.DEFAULT_IMAGE, docker_group_gid=988)
    assert "--user 0:0" in unit
    assert minimal_config.ops.zabbix.userparams_file in unit
    assert f"Description={minimal_config.meta.name}:" in unit


def test_run_zabbix_deploy_install_force_without_zbx(monkeypatch, minimal_config) -> None:
    called: dict[str, object] = {}

    def fake_install(**kwargs):
        called.update(kwargs)
        return 0

    monkeypatch.setattr(zs, "cmd_install", fake_install)
    rc = zs.run_zabbix_deploy(
        ["install", "--force"],
        config=minimal_config,
        prog="dk staging zabbix",
        ssh_target=None,
        dry_run=False,
        env_defaults={},
        site_origin="https://app.example.com",
    )
    assert rc == 0
    assert called.get("env_defaults") == {}
