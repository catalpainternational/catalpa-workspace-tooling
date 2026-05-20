"""Register deploy-host SSH keys in OpenSSH known_hosts (for DOCKER_HOST=ssh:// and BatchMode ssh)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from catalpa_tooling.dns_resolve import _docker_host_hostname
from catalpa_tooling.run_cmd import run as run_cmd


def known_hosts_path() -> Path:
    """Path to the known_hosts file (``SSH_KNOWN_HOSTS`` or ``~/.ssh/known_hosts``)."""
    raw = (os.environ.get("SSH_KNOWN_HOSTS") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".ssh" / "known_hosts"


def ssh_host_from_docker_host(docker_host: str) -> str | None:
    """Return hostname or IP for SSH, or None if ``docker_host`` is not an SSH remote."""
    raw = (docker_host or "").strip()
    if not raw:
        return None
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme and parsed.scheme != "ssh":
            return None
    host = _docker_host_hostname(raw)
    return host or None


def _ssh_port_from_docker_host(docker_host: str) -> int:
    raw = (docker_host or "").strip()
    if not raw:
        return 22
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme == "ssh" and parsed.port:
            return int(parsed.port)
    if "@" in raw and ":" in raw.split("@", 1)[1]:
        tail = raw.split("@", 1)[1]
        if tail.startswith("[") and "]:" in tail:
            port_s = tail.split("]:", 1)[1]
        elif ":" in tail and not tail.startswith("["):
            port_s = tail.rsplit(":", 1)[1]
        else:
            return 22
        try:
            return int(port_s)
        except ValueError:
            return 22
    return 22


def host_in_known_hosts(host: str, path: Path | None = None) -> bool:
    """True if ``ssh-keygen -F host`` finds an entry in known_hosts."""
    h = (host or "").strip()
    if not h:
        return False
    kh = path or known_hosts_path()
    r = run_cmd(
        ["ssh-keygen", "-F", h, "-f", str(kh)],
        check=False,
        capture_output=True,
        print_cmd=False,
    )
    return r.returncode == 0


def ensure_known_host(
    host: str,
    *,
    port: int = 22,
    dry_run: bool = False,
    known_hosts: Path | None = None,
) -> int:
    """Append ``host`` keys via ``ssh-keyscan`` if not already in known_hosts. Returns 0 on success."""
    h = (host or "").strip()
    if not h:
        return 0
    kh = known_hosts or known_hosts_path()
    if host_in_known_hosts(h, kh):
        return 0

    if dry_run:
        print(
            f"dry-run: would register SSH host key for {h!r} (port {port}) in {kh}",
            file=sys.stderr,
        )
        return 0

    ssh_dir = kh.parent
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    scan = run_cmd(
        ["ssh-keyscan", "-p", str(port), "-H", h],
        check=False,
        capture_output=True,
        text=True,
        print_cmd=False,
    )
    if scan.returncode != 0:
        err = (scan.stderr or scan.stdout or "").strip()
        print(
            f"ssh-keyscan failed for {h!r} (port {port}): {err or f'exit {scan.returncode}'}",
            file=sys.stderr,
        )
        print(
            "Ensure the host is reachable and openssh-client (ssh-keyscan) is installed.",
            file=sys.stderr,
        )
        return scan.returncode or 1

    lines = [ln for ln in (scan.stdout or "").splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        print(
            f"ssh-keyscan returned no keys for {h!r} (port {port}).",
            file=sys.stderr,
        )
        return 1

    kh.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(kh, "a", encoding="utf-8") as f:
        if kh.exists() and kh.stat().st_size > 0:
            f.write("\n")
        f.write("\n".join(lines))
        f.write("\n")
    try:
        kh.chmod(0o644)
    except OSError:
        pass

    print(f"Registered SSH host key for {h!r} in {kh}", file=sys.stderr)
    return 0


def ensure_ssh_known_host_for_docker_host(
    docker_host: str,
    *,
    dry_run: bool = False,
    known_hosts: Path | None = None,
) -> int:
    """Ensure OpenSSH knows the host in ``docker_host`` (``ssh://…`` only)."""
    host = ssh_host_from_docker_host(docker_host)
    if not host:
        return 0
    port = _ssh_port_from_docker_host(docker_host)
    return ensure_known_host(host, port=port, dry_run=dry_run, known_hosts=known_hosts)
