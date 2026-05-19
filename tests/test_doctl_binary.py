"""Tests for doctl binary resolution."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from catalpa_tooling.doctl_binary import DoctlNotFoundError, resolve_doctl_binary


def _write_executable(path: Path, content: str = "#!/bin/sh\necho doctl\n") -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_resolve_doctl_bin_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "real-doctl"
    _write_executable(real)
    monkeypatch.setenv("DOCTL_BIN", str(real))
    assert resolve_doctl_binary() == real.resolve()


def test_resolve_skips_entrypoint_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "doctl"
    _write_executable(
        shim,
        content="#!/usr/bin/env python3\nfrom catalpa_tooling.doctl_cli import main\n",
    )
    real_doctl = tmp_path / "other" / "doctl"
    real_doctl.parent.mkdir()
    _write_executable(real_doctl)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{real_doctl.parent}")
    monkeypatch.setattr(sys, "argv", [str(shim), "droplets", "list"])
    assert resolve_doctl_binary() == real_doctl.resolve()


def test_resolve_skips_shim_when_invoked_via_dk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``dk <env> host`` must not call the venv ``doctl`` wrapper on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "doctl"
    _write_executable(
        shim,
        content="#!/usr/bin/env python3\nfrom catalpa_tooling.doctl_cli import main\n",
    )
    real_doctl = tmp_path / "other" / "doctl"
    real_doctl.parent.mkdir()
    _write_executable(real_doctl)

    dk = tmp_path / "bin" / "dk"
    _write_executable(dk, content="#!/bin/sh\necho dk\n")

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{real_doctl.parent}")
    monkeypatch.setattr(sys, "argv", [str(dk), "prod", "host", "--write"])
    assert resolve_doctl_binary() == real_doctl.resolve()


def test_resolve_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCTL_BIN", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(sys, "argv", ["doctl"])
    with pytest.raises(DoctlNotFoundError, match="Install"):
        resolve_doctl_binary()
