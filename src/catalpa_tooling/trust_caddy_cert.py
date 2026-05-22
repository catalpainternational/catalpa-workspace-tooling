"""Trust Caddy's local development CA in the macOS system keychain."""

from __future__ import annotations

import sys
import tempfile
from typing import TYPE_CHECKING

from catalpa_tooling.compose import _compose
from catalpa_tooling.run_cmd import run as run_cmd

if TYPE_CHECKING:
    from catalpa_tooling.config import ProjectConfig

# Default path for Caddy's internally generated local PKI root certificate.
CADDY_LOCAL_CA_PATH = "/data/caddy/pki/authorities/local/root.crt"

_MACOS_KEYCHAIN = "/Library/Keychains/System.keychain"


def _proxy_container_id(
    compose_file: str,
    proxy_service: str,
    env_add: dict[str, str],
) -> str:
    result = _compose(
        compose_file,
        "ps",
        "-q",
        proxy_service,
        env_add=env_add,
        check=False,
        print_cmd=False,
        capture_output=True,
    )
    container_id = (result.stdout or "").strip().splitlines()
    if container_id:
        return container_id[0].strip()
    fallback = run_cmd(
        ["docker", "ps", "-q", "-f", f"name={proxy_service}"],
        check=False,
        print_cmd=False,
        capture_output=True,
        text=True,
    )
    lines = (fallback.stdout or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def trust_caddy_local_ca(
    compose_file: str,
    env_add: dict[str, str],
    config: ProjectConfig,
    *,
    dry_run: bool = False,
) -> int:
    """Copy Caddy's local CA from the proxy container and trust it on macOS."""
    if sys.platform != "darwin":
        print(
            "trust-caddy-cert is supported on macOS only (uses security add-trusted-cert). "
            "PRs for Linux support are welcome.",
            file=sys.stderr,
        )
        return 1

    proxy_service = config.stack.services.proxy
    project_name = env_add.get("COMPOSE_PROJECT_NAME", "")

    if dry_run:
        container_id = _proxy_container_id(compose_file, proxy_service, env_add)
        print(
            f"dry-run: would trust Caddy local CA (macOS System keychain). "
            f"compose_file={compose_file!r} "
            f"COMPOSE_PROJECT_NAME={project_name!r} "
            f"proxy_service={proxy_service!r} "
            f"container_id={container_id or '(not running)'}",
            file=sys.stderr,
        )
        if container_id:
            print(
                f"dry-run: would run docker cp {container_id}:{CADDY_LOCAL_CA_PATH} <tmp> "
                f"then sudo security add-trusted-cert -d -r trustRoot -k {_MACOS_KEYCHAIN} <tmp>",
                file=sys.stderr,
            )
        return 0

    container_id = _proxy_container_id(compose_file, proxy_service, env_add)
    if not container_id:
        print(
            f"Error: {proxy_service!r} container not running for compose file {compose_file!r}. "
            "Start the stack (e.g. uv run dk <env> up -d).",
            file=sys.stderr,
        )
        return 1

    with tempfile.NamedTemporaryFile(
        prefix="catalpa-caddy-root.",
        delete=False,
    ) as tmp:
        cert_dest = tmp.name

    try:
        print(f"Copying Caddy root CA from container {container_id}...")
        cp = run_cmd(
            ["docker", "cp", f"{container_id}:{CADDY_LOCAL_CA_PATH}", cert_dest],
            check=False,
        )
        if cp.returncode != 0:
            return cp.returncode

        print("Adding certificate as trusted root (requires sudo)...")
        trust = run_cmd(
            [
                "sudo",
                "security",
                "add-trusted-cert",
                "-d",
                "-r",
                "trustRoot",
                "-k",
                _MACOS_KEYCHAIN,
                cert_dest,
            ],
            check=False,
        )
        if trust.returncode != 0:
            return trust.returncode

        print("Done. Caddy certificates for local development hosts should now be trusted.")
        return 0
    finally:
        from pathlib import Path

        Path(cert_dest).unlink(missing_ok=True)
