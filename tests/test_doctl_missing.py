"""Graceful behaviour when host ``doctl`` is not installed."""

from __future__ import annotations

import pytest

from catalpa_tooling import doctl_cli
from catalpa_tooling.doctl_binary import DoctlNotFoundError, resolve_doctl_binary


def test_run_digoc_cloud_config_without_doctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.resolve_doctl_binary",
        lambda: (_ for _ in ()).throw(DoctlNotFoundError("missing")),
    )
    assert doctl_cli.run_digoc(["cloud-config", "print"]) == 0


def test_run_digoc_auth_init_without_doctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.resolve_doctl_binary",
        lambda: (_ for _ in ()).throw(DoctlNotFoundError("missing")),
    )
    assert doctl_cli.run_digoc(["auth", "init"]) == 1


def test_run_digoc_droplets_create_dry_run_without_doctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.doctl_binary.resolve_doctl_binary",
        lambda: (_ for _ in ()).throw(DoctlNotFoundError("missing")),
    )
    monkeypatch.setattr(
        doctl_cli,
        "_load_do_config_for_droplets",
        lambda project_flag: (None, None),
    )
    assert (
        doctl_cli.run_digoc(
            [
                "droplets",
                "create",
                "my-host",
                "--size",
                "s-1vcpu-1gb",
                "--region",
                "sgp1",
                "--project",
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "--dry-run",
            ]
        )
        == 0
    )


def test_ensure_doctl_raises_not_systemexit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCTL_BIN", "")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("sys.argv", ["dk", "digoc"])
    with pytest.raises(DoctlNotFoundError):
        resolve_doctl_binary()
