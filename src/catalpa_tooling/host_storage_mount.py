"""SSH helpers to format and mount block storage on deploy hosts."""

from __future__ import annotations

import shlex
import sys

from catalpa_tooling.run_cmd import format_shell_command, run as run_cmd
from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target


def do_volume_device_path(volume_name: str) -> str:
    """Stable device path for a DO block volume on Linux."""
    safe = volume_name.strip().replace(" ", "_")
    return f"/dev/disk/by-id/scsi-0DO_Volume_{safe}"


def _ssh_run(ssh_target: str, script: str, *, label: str = "storage") -> int:
    cmd = ["ssh", ssh_target, script]
    print(f"$ {format_shell_command(cmd)}", file=sys.stderr)
    r = run_cmd(cmd, check=False, print_cmd=False)
    if r.returncode != 0:
        print(f"{label}: remote command failed (exit {r.returncode}).", file=sys.stderr)
    return r.returncode


def mount_do_volume_at_path(
    ssh_target: str,
    volume_name: str,
    mount_path: str,
    *,
    filesystem: str = "ext4",
    label: str = "storage",
) -> int:
    """Format (if needed), mount, and fstab a DO block volume at ``mount_path``. Idempotent."""
    device = do_volume_device_path(volume_name)
    mount = mount_path.rstrip("/") or "/"
    q_device = shlex.quote(device)
    q_mount = shlex.quote(mount)
    q_fs = shlex.quote(filesystem)
    script = (
        "set -euo pipefail\n"
        f"DEVICE={q_device}\n"
        f"MOUNT={q_mount}\n"
        f"FS={q_fs}\n"
        "if ! test -e \"$DEVICE\"; then\n"
        "  echo \"device $DEVICE not found (volume not attached yet?)\" >&2\n"
        "  exit 1\n"
        "fi\n"
        "if mountpoint -q \"$MOUNT\"; then\n"
        "  echo \"already mounted at $MOUNT\"\n"
        "  exit 0\n"
        "fi\n"
        "mkdir -p \"$MOUNT\"\n"
        "if ! blkid \"$DEVICE\" >/dev/null 2>&1; then\n"
        "  echo \"formatting $DEVICE as $FS\"\n"
        "  mkfs -t \"$FS\" \"$DEVICE\"\n"
        "fi\n"
        "mount \"$DEVICE\" \"$MOUNT\"\n"
        "FSTAB_LINE=\"$DEVICE $MOUNT $FS defaults,nofail,discard 0 2\"\n"
        "if ! grep -qF \"$FSTAB_LINE\" /etc/fstab; then\n"
        "  echo \"$FSTAB_LINE\" >> /etc/fstab\n"
        "fi\n"
    )
    return _ssh_run(ssh_target, script, label=label)


def verify_host_mount_path(
    ssh_target: str,
    mount_path: str,
    *,
    label: str = "storage",
) -> int:
    """Verify ``mount_path`` exists and is writable (path-only storage entries)."""
    mount = mount_path.rstrip("/") or "/"
    q = shlex.quote(mount)
    script = f"test -d {q} && test -w {q}"
    cmd = ["ssh", ssh_target, script]
    print(f"$ {format_shell_command(cmd)}", file=sys.stderr)
    r = run_cmd(cmd, check=False, print_cmd=False)
    if r.returncode != 0:
        print(
            f"{label}: host path {mount!r} is missing or not writable on {ssh_target}.",
            file=sys.stderr,
        )
    return r.returncode


def ssh_target_from_docker_host(docker_host: str) -> str | None:
    """Return SSH target or ``None`` when ``docker_host`` is not SSH-shaped."""
    raw = (docker_host or "").strip()
    if not raw:
        return None
    try:
        return parse_docker_host_to_ssh_target(raw)
    except ValueError:
        return None


def verify_host_mount_path_for_docker_host(
    docker_host: str,
    mount_path: str,
    *,
    label: str = "storage",
) -> int:
    """Path-only verify using ``docker_host`` from info.yaml."""
    target = ssh_target_from_docker_host(docker_host)
    if target is None:
        mount = mount_path.rstrip("/") or "/"
        import os
        from pathlib import Path

        p = Path(mount)
        if p.is_dir() and os.access(p, os.W_OK):
            return 0
        print(
            f"{label}: host path {mount!r} is missing or not writable locally.",
            file=sys.stderr,
        )
        return 1
    return verify_host_mount_path(target, mount_path, label=label)
