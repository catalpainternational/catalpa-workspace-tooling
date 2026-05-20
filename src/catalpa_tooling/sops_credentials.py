"""Update SOPS-encrypted ``credentials.yaml`` via ``sops set``."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from catalpa_tooling.env_yaml import _credentials_to_env
from catalpa_tooling.run_cmd import run as run_cmd

_CREDENTIAL_ENV_PREFIXES = ("PGBR_", "RESTIC_")


class SopsNotFoundError(RuntimeError):
    """``sops`` binary is missing from PATH."""


class SopsCommandError(RuntimeError):
    """``sops set`` or ``sops -d`` failed."""

    def __init__(self, message: str, *, returncode: int) -> None:
        super().__init__(message)
        self.returncode = returncode


def ensure_sops_available() -> None:
    if not shutil.which("sops"):
        raise SopsNotFoundError(
            "sops is required to update credentials.yaml. "
            "Install sops: https://github.com/getsops/sops"
        )


def sops_set(creds_path: Path, key: str, value: str) -> None:
    """Set one top-level key in an encrypted credentials file."""
    ensure_sops_available()
    result = run_cmd(
        [
            "sops",
            "set",
            str(creds_path),
            json.dumps([key]),
            json.dumps(value),
        ],
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip() or f"sops set failed for {key!r}"
        raise SopsCommandError(err, returncode=result.returncode)


def decrypt_credentials_yaml(creds_path: Path) -> dict:
    """Decrypt ``credentials.yaml`` and return the mapping (may be empty)."""
    ensure_sops_available()
    result = run_cmd(
        ["sops", "-d", str(creds_path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip() or "sops decrypt failed"
        raise SopsCommandError(err, returncode=result.returncode)
    data = yaml.safe_load(result.stdout) or {}
    if not isinstance(data, dict):
        return {}
    return data


def apply_credential_sets(creds_path: Path, values: dict[str, str]) -> None:
    """Apply multiple ``sops set`` updates in order."""
    for key, value in values.items():
        try:
            sops_set(creds_path, key, value)
        except SopsCommandError as e:
            print(f"Failed to set {key!r} in {creds_path}: {e}", file=sys.stderr)
            raise


def refresh_env_credentials(env_add: dict[str, str], creds_path: Path) -> None:
    """Re-decrypt credentials and merge into ``env_add`` (replacing prior credential keys)."""
    creds = decrypt_credentials_yaml(creds_path)
    for k in list(env_add):
        if k.startswith(_CREDENTIAL_ENV_PREFIXES):
            del env_add[k]
    env_add.update(_credentials_to_env(creds))
