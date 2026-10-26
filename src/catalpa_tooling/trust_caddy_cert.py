"""Trust Caddy's local development CA on the host (macOS or Linux)."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from catalpa_tooling.compose import _compose
from catalpa_tooling.local_proxy import LOCAL_PROXY_CONTAINER, local_proxy_enabled, proxy_container_id
from catalpa_tooling.run_cmd import run as run_cmd

if TYPE_CHECKING:
    from catalpa_tooling.config import ProjectConfig

# Default path for Caddy's internally generated local PKI root certificate.
CADDY_LOCAL_CA_PATH = "/data/caddy/pki/authorities/local/root.crt"

_MACOS_KEYCHAIN = "/Library/Keychains/System.keychain"
_LINUX_CA_BASENAME = "catalpa-local-proxy-root.crt"
# Each Linux family's system trust store: the command that rebuilds it, and the folder it reads.
# Debian and Ubuntu use update-ca-certificates; Fedora and its relatives (RHEL, Silverblue,
# Bluefin) use p11-kit's update-ca-trust, and have no update-ca-certificates at all.
_LINUX_TRUST_STORES = (
    ("update-ca-certificates", Path("/usr/local/share/ca-certificates")),
    ("update-ca-trust", Path("/etc/pki/ca-trust/source/anchors")),
)


def _compose_proxy_container_id(
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


def resolve_trust_caddy_container(
    compose_file: str,
    env_add: dict[str, str],
    config: ProjectConfig,
    info: dict | None,
) -> str:
    """Return a running Caddy container id for CA export (local proxy or stack proxy)."""
    if info is not None and local_proxy_enabled(info):
        cid = proxy_container_id()
        if cid:
            return cid
    proxy_service = config.stack.services.proxy
    return _compose_proxy_container_id(compose_file, proxy_service, env_add)


def _trust_macos(cert_dest: str, *, dry_run: bool) -> int:
    if dry_run:
        print(
            f"dry-run: would run sudo security add-trusted-cert -d -r trustRoot "
            f"-k {_MACOS_KEYCHAIN} {cert_dest}",
            file=sys.stderr,
        )
        return 0
    print("Adding certificate as trusted root (requires sudo)...", file=sys.stderr)
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
    return trust.returncode


def _trust_linux(cert_dest: str, *, dry_run: bool) -> int:
    # Both the command and its folder: Arch has update-ca-trust without Fedora's anchors folder,
    # openSUSE update-ca-certificates without Debian's.
    store = next(
        ((cmd, d) for cmd, d in _LINUX_TRUST_STORES if shutil.which(cmd) and d.is_dir()), None
    )
    if store is None:
        names = " nor ".join(f"{cmd} with {d}" for cmd, d in _LINUX_TRUST_STORES)
        print(
            f"Cannot trust the CA: this system has neither {names}. Add the root to its "
            "trust store by hand (dk's local proxy serves it at "
            "http://localhost/catalpa-local-ca.crt), or import it into the browser.",
            file=sys.stderr,
        )
        return 1
    update, ca_dir = store
    dest = ca_dir / _LINUX_CA_BASENAME
    if dry_run:
        print(
            f"dry-run: would copy {cert_dest} to {dest} and run sudo {update}",
            file=sys.stderr,
        )
        return 0
    print(f"Installing CA to {dest} (requires sudo)...", file=sys.stderr)
    # Readable by everyone: on Fedora, browsers read the anchors folder as the user (p11-kit),
    # and the temporary copy is private to us.
    install = run_cmd(
        ["sudo", "install", "-m", "0644", cert_dest, str(dest)],
        check=False,
    )
    if install.returncode != 0:
        return install.returncode
    updated = run_cmd(["sudo", update], check=False)
    return updated.returncode


def trust_caddy_ca_from_container(
    container_id: str,
    *,
    dry_run: bool = False,
) -> int:
    """Copy Caddy's local CA from ``container_id`` and trust it on the host."""
    if not container_id:
        print(
            f"Error: no running Caddy container found ({LOCAL_PROXY_CONTAINER} or stack proxy). "
            "Start the local proxy (`dk proxy up`) or the stack (`dk <env> up -d`).",
            file=sys.stderr,
        )
        return 1

    if dry_run:
        print(
            f"dry-run: would trust Caddy local CA from container {container_id!r} "
            f"({CADDY_LOCAL_CA_PATH}) on {sys.platform}",
            file=sys.stderr,
        )
        return 0

    with tempfile.NamedTemporaryFile(
        prefix="catalpa-caddy-root.",
        delete=False,
    ) as tmp:
        cert_dest = tmp.name

    try:
        print(f"Copying Caddy root CA from container {container_id}...", file=sys.stderr)
        cp = run_cmd(
            ["docker", "cp", f"{container_id}:{CADDY_LOCAL_CA_PATH}", cert_dest],
            check=False,
        )
        if cp.returncode != 0:
            return cp.returncode

        if sys.platform == "darwin":
            rc = _trust_macos(cert_dest, dry_run=False)
        elif sys.platform == "linux":
            rc = _trust_linux(cert_dest, dry_run=False)
        else:
            print(
                f"trust-caddy-cert is supported on macOS and Linux only (platform={sys.platform!r}).",
                file=sys.stderr,
            )
            return 1

        if rc == 0:
            print(
                "Done. Caddy certificates for local development hosts should now be trusted. "
                "Restart your browser if HTTPS still warns.",
                file=sys.stderr,
            )
        return rc
    finally:
        Path(cert_dest).unlink(missing_ok=True)


def trust_caddy_local_ca(
    compose_file: str,
    env_add: dict[str, str],
    config: ProjectConfig,
    *,
    dry_run: bool = False,
    info: dict | None = None,
) -> int:
    """Trust Caddy's local CA from the local dev proxy or the stack proxy container."""
    container_id = resolve_trust_caddy_container(compose_file, env_add, config, info)
    project_name = env_add.get("COMPOSE_PROJECT_NAME", "")

    if dry_run:
        source = LOCAL_PROXY_CONTAINER if info and local_proxy_enabled(info) else config.stack.services.proxy
        print(
            f"dry-run: would trust Caddy local CA from {source!r} "
            f"(container_id={container_id or '(not running)'}, "
            f"compose_file={compose_file!r}, COMPOSE_PROJECT_NAME={project_name!r})",
            file=sys.stderr,
        )
        return trust_caddy_ca_from_container(container_id, dry_run=True)

    if not container_id:
        if info and local_proxy_enabled(info):
            print(
                f"Error: {LOCAL_PROXY_CONTAINER!r} is not running. "
                "Start it with `dk proxy up` or `dk <env> up -d`.",
                file=sys.stderr,
            )
        else:
            proxy_service = config.stack.services.proxy
            print(
                f"Error: {proxy_service!r} container not running for compose file {compose_file!r}. "
                "Start the stack (e.g. `uv run dk <env> up -d`).",
                file=sys.stderr,
            )
        return 1

    return trust_caddy_ca_from_container(container_id, dry_run=False)
