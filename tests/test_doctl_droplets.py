"""Tests for droplet creation with cloud-config."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from catalpa_tooling.config import DigitalOceanConfig
from catalpa_tooling.doctl_binary import DoctlCommandError
from catalpa_tooling.doctl_droplets import (
    _resolve_ssh_keys,
    create_droplet,
    list_account_ssh_key_ids,
)


@pytest.fixture(autouse=True)
def _no_existing_droplet_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.find_project_droplet_id_by_name",
        lambda *_args, **_kwargs: None,
    )


def test_create_droplet_missing_size(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exc:
        create_droplet(
            "host",
            project_id="proj-1",
            ssh_keys=("key-1",),
            do_config=None,
        )
    assert exc.value.code == 1
    assert "size" in capsys.readouterr().err.lower()


def test_list_account_ssh_key_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.run_doctl_json",
        lambda *args, context=None: [
            {"id": 111, "fingerprint": "aa:bb"},
            {"id": 222, "fingerprint": "cc:dd"},
        ],
    )
    assert list_account_ssh_key_ids() == ("111", "222")


def test_list_account_ssh_key_ids_403_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    def fake_json(*_args, context=None):
        raise DoctlCommandError(
            "GET https://api.digitalocean.com/v2/account/keys: 403 not authorized",
            returncode=403,
        )

    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl_json", fake_json)
    with pytest.raises(DoctlCommandError):
        list_account_ssh_key_ids()
    err = capsys.readouterr().err
    assert "ssh_key:read" in err
    assert "account:read" in err


def test_resolve_ssh_keys_defaults_to_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_droplets.list_account_ssh_key_ids",
        lambda *, context: ("10", "20"),
    )
    assert _resolve_ssh_keys((), None, context=None) == ("10", "20")


def test_resolve_ssh_keys_cli_overrides_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_droplets.list_account_ssh_key_ids",
        lambda *, context: pytest.fail("should not list when CLI keys set"),
    )
    assert _resolve_ssh_keys(("only-cli",), None, context=None) == ("only-cli",)


def test_resolve_ssh_keys_manifest_overrides_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_droplets.list_account_ssh_key_ids",
        lambda *, context: pytest.fail("should not list when manifest keys set"),
    )
    do = DigitalOceanConfig(
        project_name=None,
        project_id=None,
        context=None,
        timezone=None,
        region=None,
        size=None,
        image=None,
        ssh_keys=("from-yaml",),
    )
    assert _resolve_ssh_keys((), do, context=None) == ("from-yaml",)


def test_create_droplet_missing_ssh_keys(capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_droplets.list_account_ssh_key_ids",
        lambda *, context: (),
    )
    with pytest.raises(SystemExit) as exc:
        create_droplet(
            "host",
            size="s-1vcpu-1gb",
            region="sgp1",
            project_id="proj-1",
            do_config=None,
        )
    assert exc.value.code == 1
    assert "no ssh keys" in capsys.readouterr().err.lower()


def test_create_droplet_dry_run_lists_account_keys_when_doctl_available(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_droplets.list_account_ssh_key_ids",
        lambda *, context: ("111", "222"),
    )
    mock_run = MagicMock()
    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl", mock_run)

    rc = create_droplet(
        "my-host",
        size="s-1vcpu-1gb",
        region="sgp1",
        project_id="proj-uuid",
        dry_run=True,
    )
    assert rc == 0
    mock_run.assert_not_called()
    err = capsys.readouterr().err
    assert "2 SSH key" in err
    assert "--ssh-keys 111" in err
    assert "--ssh-keys 222" in err


def test_create_droplet_dry_run_without_doctl(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from catalpa_tooling.doctl_binary import DoctlNotFoundError

    monkeypatch.setattr(
        "catalpa_tooling.doctl_droplets.list_account_ssh_key_ids",
        lambda *, context: (_ for _ in ()).throw(DoctlNotFoundError("missing")),
    )
    rc = create_droplet(
        "my-host",
        size="s-1vcpu-1gb",
        region="sgp1",
        project_id="proj-uuid",
        ssh_keys=("aa:bb:cc",),
        dry_run=True,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "--ssh-keys aa:bb:cc" in err


def test_create_droplet_env_overrides_manifest_in_dry_run(
    capsys: pytest.CaptureFixture,
) -> None:
    from catalpa_tooling.config import DigitalOceanConfig

    do_config = DigitalOceanConfig(
        project_name=None,
        project_id=None,
        context=None,
        timezone=None,
        region="nyc1",
        size="s-4vcpu-8gb",
        image=None,
        ssh_keys=("aa:bb:cc",),
    )
    rc = create_droplet(
        "my-host",
        project_id="proj-uuid",
        env_size="s-1vcpu-2gb",
        env_region="sgp1",
        ssh_keys=("aa:bb:cc",),
        dry_run=True,
        do_config=do_config,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "--size s-1vcpu-2gb" in err
    assert "--region sgp1" in err


def test_create_droplet_dry_run(capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_run = MagicMock()
    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl", mock_run)

    rc = create_droplet(
        "my-host",
        size="s-1vcpu-1gb",
        region="sgp1",
        project_id="proj-uuid",
        ssh_keys=("aa:bb:cc",),
        dry_run=True,
    )
    assert rc == 0
    mock_run.assert_not_called()
    out, err = capsys.readouterr()
    assert out.startswith("#cloud-config")
    assert "compute droplet create my-host" in err
    assert "--user-data-file" in err
    assert "--ssh-keys aa:bb:cc" in err
    assert "--enable-monitoring" in err


def test_create_droplet_dry_run_no_monitoring(capsys: pytest.CaptureFixture) -> None:
    rc = create_droplet(
        "my-host",
        size="s-1vcpu-1gb",
        region="sgp1",
        project_id="proj-uuid",
        ssh_keys=("aa:bb:cc",),
        dry_run=True,
        enable_monitoring=False,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "--enable-monitoring" not in err


def test_create_droplet_dry_run_manifest_monitoring_off(
    capsys: pytest.CaptureFixture,
) -> None:
    do_config = DigitalOceanConfig(
        project_name=None,
        project_id=None,
        context=None,
        timezone=None,
        region="sgp1",
        size="s-1vcpu-1gb",
        image=None,
        ssh_keys=("aa:bb:cc",),
        monitoring=False,
    )
    rc = create_droplet(
        "my-host",
        project_id="proj-uuid",
        dry_run=True,
        do_config=do_config,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "--enable-monitoring" not in err


def test_create_droplet_rejects_duplicate_name(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_projects.find_project_droplet_id_by_name",
        lambda _pid, name, *, context: 4242 if name == "tempu-test" else None,
    )
    mock_run = MagicMock()
    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl", mock_run)

    rc = create_droplet(
        "tempu-test",
        size="s-1vcpu-1gb",
        region="sgp1",
        project_id="proj-uuid",
        ssh_keys=("key-1",),
    )
    assert rc == 1
    mock_run.assert_not_called()
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "4242" in err


def test_create_droplet_invokes_doctl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    captured_user_data: list[str] = []

    def fake_run_doctl(args, *, context=None):
        calls.append(list(args))
        user_data_idx = args.index("--user-data-file")
        captured_user_data.append(Path(args[user_data_idx + 1]).read_text(encoding="utf-8"))
        return MagicMock(returncode=0)

    def fake_ensure():
        return tmp_path / "doctl"

    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl", fake_run_doctl)
    monkeypatch.setattr("catalpa_tooling.doctl_binary.ensure_doctl_available", fake_ensure)

    do_config = DigitalOceanConfig(
        project_name="p",
        project_id=None,
        context=None,
        timezone="Asia/Dili",
        region="sgp1",
        size="s-2vcpu-4gb",
        image="ubuntu-24-04-x64",
        ssh_keys=("manifest-key",),
    )
    rc = create_droplet(
        "prod-1",
        project_id="proj-uuid",
        wait=True,
        do_config=do_config,
    )
    assert rc == 0
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0:4] == ["compute", "droplet", "create", "prod-1"]
    assert "--size" in argv and "s-2vcpu-4gb" in argv
    assert "--region" in argv and "sgp1" in argv
    assert "--project-id" in argv and "proj-uuid" in argv
    assert "--ssh-keys" in argv and "manifest-key" in argv
    assert "--enable-monitoring" in argv
    assert "--wait" in argv
    assert len(captured_user_data) == 1
    assert captured_user_data[0].startswith("#cloud-config")
