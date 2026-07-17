"""Shared SSH file install for dc-backup (TLS + stack)."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from catalpa_tooling.run_cmd import run as run_cmd


@dataclass(frozen=True)
class RemoteResult:
    """Captured stdout/stderr from a remote SSH command."""

    returncode: int
    stdout: str
    stderr: str


def install_files_via_ssh(
    ssh_target: str,
    remote_dir: str,
    files: list[tuple[str, str, int]],
    *,
    dry_run: bool,
) -> int:
    """Install ``(filename, content, mode)`` under ``remote_dir`` on ``ssh_target``."""
    if dry_run:
        names = ", ".join(f"{n} mode={m:o}" for n, _, m in files)
        print(
            f"[dry-run] would install on {ssh_target}:{remote_dir}/ → {names}",
            flush=True,
        )
        return 0

    from catalpa_tooling.ssh_known_hosts import ensure_ssh_known_host_for_docker_host

    dh = ssh_target if "://" in ssh_target else f"ssh://{ssh_target}"
    kh = ensure_ssh_known_host_for_docker_host(dh)
    if kh != 0:
        print(f"Could not register SSH host key for {ssh_target!r}.", file=sys.stderr)
        return 1

    mkdir = run_cmd(
        ["ssh", "-o", "BatchMode=yes", ssh_target, f"sudo mkdir -p {remote_dir}"],
        check=False,
        print_cmd=True,
    )
    if mkdir.returncode != 0:
        return int(mkdir.returncode or 1)

    with tempfile.TemporaryDirectory(prefix="catalpa-dc-backup-") as td:
        local_dir = Path(td)
        local_paths: list[Path] = []
        for name, content, _mode in files:
            p = local_dir / name
            p.write_text(content, encoding="utf-8")
            p.chmod(0o600)
            local_paths.append(p)
        remote_stage = f"/tmp/catalpa-dc-backup-{Path(td).name}"
        stage_mkdir = run_cmd(
            ["ssh", "-o", "BatchMode=yes", ssh_target, f"mkdir -p {remote_stage}"],
            check=False,
            print_cmd=True,
        )
        if stage_mkdir.returncode != 0:
            return int(stage_mkdir.returncode or 1)
        scp = run_cmd(
            [
                "scp",
                "-q",
                "-o",
                "BatchMode=yes",
                *[str(p) for p in local_paths],
                f"{ssh_target}:{remote_stage}/",
            ],
            check=False,
            print_cmd=True,
        )
        if scp.returncode != 0:
            return int(scp.returncode or 1)

        install_parts = [f"sudo mkdir -p {remote_dir}"]
        for name, _content, mode in files:
            install_parts.append(
                f"sudo install -m {mode:o} {remote_stage}/{name} {remote_dir}/{name}"
            )
        install_parts.append(f"rm -rf {remote_stage}")
        fin = run_cmd(
            ["ssh", "-o", "BatchMode=yes", ssh_target, " && ".join(install_parts)],
            check=False,
            print_cmd=True,
        )
        return int(fin.returncode or 0)


def remote_path_exists(ssh_target: str, remote_path: str) -> bool | None:
    """True/False when SSH works; None on connection/command failure."""
    r = run_cmd(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            ssh_target,
            f"test -f {remote_path}",
        ],
        check=False,
        print_cmd=False,
        capture_output=True,
        text=True,
    )
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    return None


def remote_run(ssh_target: str, remote_cmd: str, *, dry_run: bool = False) -> int:
    if dry_run:
        print(f"[dry-run] ssh {ssh_target} {remote_cmd!r}", flush=True)
        return 0
    r = run_cmd(
        ["ssh", "-o", "BatchMode=yes", ssh_target, remote_cmd],
        check=False,
        print_cmd=True,
    )
    return int(r.returncode or 0)


def remote_run_capture(
    ssh_target: str,
    remote_cmd: str,
    *,
    dry_run: bool = False,
) -> RemoteResult:
    """Run ``remote_cmd`` on ``ssh_target`` and return stdout/stderr."""
    if dry_run:
        print(f"[dry-run] ssh {ssh_target} {remote_cmd!r}", flush=True)
        return RemoteResult(0, "", "")
    r = run_cmd(
        ["ssh", "-o", "BatchMode=yes", ssh_target, remote_cmd],
        check=False,
        print_cmd=True,
        capture_output=True,
        text=True,
    )
    return RemoteResult(
        int(r.returncode or 0),
        r.stdout or "",
        r.stderr or "",
    )
