"""Rsync helpers for ``django_media`` volume sync (fetch + ``bkp_files push``)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from catalpa_tooling.restic_files import _default_compose_project, django_media_volume_name
from catalpa_tooling.run_cmd import format_shell_command, run as run_cmd
from catalpa_tooling.ssh_known_hosts import (
    ensure_ssh_known_host_for_ssh_target,
    is_ssh_host_key_verification_error,
    print_ssh_host_key_hint,
)
from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

if TYPE_CHECKING:
    from catalpa_tooling.config import ProjectConfig

# Like ``rsync -az`` but omit ``-l`` (--links): symbolic links on the server are skipped.
RSYNC_BASE: tuple[str, ...] = ("rsync", "-rptgoDzv", "--progress")

PushMediaMethod = Literal["rsync", "tar"]


def ssh_target_from_host(host: str) -> str:
    """Normalize bare hostname to ``root@host``; leave ``user@host`` unchanged."""
    host = host.strip()
    if "@" in host:
        return host
    return f"root@{host}"


def try_ssh_target_from_docker_host(docker_host: str) -> str | None:
    """Return ``user@host`` when ``docker_host`` is SSH-shaped; else ``None`` (local socket)."""
    try:
        return parse_docker_host_to_ssh_target(docker_host)
    except ValueError:
        return None


def _parse_volume_mountpoint_json(stdout: str, volume_name: str, *, label: str) -> str:
    try:
        payload = json.loads(stdout or "[]")
    except json.JSONDecodeError as exc:
        print(
            f"{label}: invalid JSON from docker volume inspect for {volume_name!r}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    if not isinstance(payload, list) or not payload:
        print(f"{label}: volume {volume_name!r} not found.", file=sys.stderr)
        raise SystemExit(1)
    first = payload[0]
    if not isinstance(first, dict):
        print(f"{label}: unexpected inspect payload for {volume_name!r}.", file=sys.stderr)
        raise SystemExit(1)
    mount = str(first.get("Mountpoint") or "").strip()
    if not mount:
        print(
            f"{label}: empty Mountpoint from docker volume inspect for {volume_name!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return mount.rstrip("/")


def docker_volume_mountpoint_ssh(ssh_target: str, volume_name: str, *, label: str = "media") -> str:
    """``docker volume inspect`` on a remote host via SSH; return mount path."""
    cmd = ["ssh", ssh_target, "docker", "volume", "inspect", volume_name]
    print(f"$ {format_shell_command(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        print(
            f"{label}: docker volume inspect failed for {volume_name!r} on {ssh_target}.",
            file=sys.stderr,
        )
        if err:
            print(err, file=sys.stderr)
        if is_ssh_host_key_verification_error(err):
            print_ssh_host_key_hint(ssh_target=ssh_target)
        raise SystemExit(proc.returncode or 1)
    return _parse_volume_mountpoint_json(proc.stdout or "", volume_name, label=label)


def docker_volume_mountpoint_local(
    env: dict[str, str],
    volume_name: str,
    *,
    label: str = "media",
) -> str:
    """``docker volume inspect`` using ``DOCKER_HOST`` from ``env``; return mount path."""
    run_env = os.environ.copy()
    for k, v in env.items():
        if v is not None:
            run_env[k] = str(v)
    cmd = ["docker", "volume", "inspect", volume_name]
    print(f"$ {format_shell_command(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=run_env, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        print(
            f"{label}: docker volume inspect failed for {volume_name!r}.",
            file=sys.stderr,
        )
        if err:
            print(err, file=sys.stderr)
        raise SystemExit(proc.returncode or 1)
    return _parse_volume_mountpoint_json(proc.stdout or "", volume_name, label=label)


def mountpoint_host_rsync_writable(mount: str) -> bool:
    """True when the host can rsync directly into the volume mount (typical Linux, not macOS VM paths)."""
    if sys.platform == "darwin":
        return False
    p = Path(mount)
    try:
        return p.is_dir() and os.access(p, os.W_OK)
    except OSError:
        return False


def rsync_pull_remote_to_local(ssh_target: str, remote_path: str, local_path: Path) -> int:
    """Rsync from ``ssh:remote_path`` into ``local_path`` (``dev fetch media``)."""
    local_path.mkdir(parents=True, exist_ok=True)
    remote = f"{ssh_target}:{remote_path}"
    cmd = [*RSYNC_BASE, remote, str(local_path)]
    print(f"$ {format_shell_command(cmd)}", file=sys.stderr)
    return run_cmd(cmd, check=False, print_cmd=False).returncode


def rsync_push_local_to_dest(
    source: Path,
    dest: str,
    *,
    delete: bool,
    dry_run: bool,
) -> int:
    """Rsync ``source/`` to ``dest`` (local path or ``user@host:path``)."""
    src = str(source.resolve())
    if not src.endswith(os.sep):
        src = src + os.sep
    dest_norm = dest if dest.endswith("/") else dest + "/"
    cmd: list[str] = list(RSYNC_BASE)
    if delete:
        cmd.append("--delete")
    if dry_run:
        cmd.extend(["--dry-run", "--itemize-changes"])
    cmd.extend([src, dest_norm])
    print(f"$ {format_shell_command(cmd)}", file=sys.stderr)
    return run_cmd(cmd, check=False, print_cmd=False).returncode


def rsync_push_via_container(
    env: dict[str, str],
    volume_name: str,
    source: Path,
    *,
    delete: bool,
    dry_run: bool,
    alpine_image: str = "alpine:3.21",
) -> int:
    """Rsync host ``source`` into ``volume_name`` via one-off Alpine (macOS / inaccessible mount)."""
    run_env = os.environ.copy()
    for k, v in env.items():
        if v is not None:
            run_env[k] = str(v)
    local_env = os.environ.copy()
    local_env.pop("DOCKER_HOST", None)

    delete_flag = " --delete" if delete else ""
    inner = (
        "apk add --no-cache rsync >/dev/null && "
        f"rsync -a{delete_flag} /src/ /data/"
    )
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "-v",
        f"{volume_name}:/data",
        "-v",
        f"{source.resolve()}:/src:ro",
        alpine_image,
        "sh",
        "-c",
        inner,
    ]
    if dry_run:
        print(
            f"dry-run: container rsync {source.resolve()} -> volume {volume_name!r}",
            file=sys.stderr,
        )
        print(f"dry-run: {' '.join(docker_cmd)}", file=sys.stderr)
        return 0

    return run_cmd(docker_cmd, env=run_env, check=False).returncode


def resolve_push_media_source(
    config: ProjectConfig,
    repo_root: Path,
    raw: str | None,
) -> Path | None:
    """Resolve host media directory for ``bkp_files push`` (default ``native.fetch_media.dest``)."""
    if raw:
        p = Path(raw).expanduser()
        path = p.resolve() if p.is_absolute() else (repo_root / p).resolve()
        if not p.is_absolute():
            try:
                path.relative_to(repo_root.resolve())
            except ValueError:
                print("Path must stay under the repository.", file=sys.stderr)
                return None
        if not path.is_dir():
            print(f"bkp_files push: not a directory: {path}", file=sys.stderr)
            return None
        if not any(path.iterdir()):
            print(f"bkp_files push: directory is empty: {path}", file=sys.stderr)
            return None
        return path

    default = config.fetch_media_dest_path
    if default.is_dir() and any(default.iterdir()):
        resolved = default.resolve()
        print(f"bkp_files push: using default source {resolved}", file=sys.stderr)
        return resolved

    print(
        f"bkp_files push: no media at {default} — run `uv run native fetch media` first "
        "or pass `--source PATH`.",
        file=sys.stderr,
    )
    return None


def run_push_media_rsync(
    env: dict[str, str],
    *,
    source: Path,
    dry_run: bool,
    method: PushMediaMethod = "rsync",
    delete: bool = True,
    alpine_image: str = "alpine:3.21",
) -> int:
    """Push host media into ``django_media`` via rsync (tar fallback on failure or ``method=tar``)."""
    from catalpa_tooling.media_pull import run_push_media

    if method == "tar":
        return run_push_media(
            env,
            source=source,
            dry_run=dry_run,
            alpine_image=alpine_image,
        )

    if not shutil.which("rsync"):
        print(
            "bkp_files push: rsync is not on PATH; use `--method tar` or install rsync.",
            file=sys.stderr,
        )
        return 1

    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip() or _default_compose_project(None)
    vol = django_media_volume_name(project)
    docker_host = str(env.get("DOCKER_HOST") or "").strip()

    ssh_target = try_ssh_target_from_docker_host(docker_host)
    if ssh_target:
        print(f"Resolving Docker volume {vol!r} on {ssh_target} …", file=sys.stderr)
        mount = docker_volume_mountpoint_ssh(ssh_target, vol, label="bkp_files push")
        dest = f"{ssh_target}:{mount}/"
        rc = rsync_push_local_to_dest(source, dest, delete=delete, dry_run=dry_run)
    else:
        mount = docker_volume_mountpoint_local(env, vol, label="bkp_files push")
        if mountpoint_host_rsync_writable(mount):
            print(f"bkp_files push: rsync → {mount}/", file=sys.stderr)
            rc = rsync_push_local_to_dest(source, mount + "/", delete=delete, dry_run=dry_run)
        else:
            print(
                f"bkp_files push: using container rsync into volume {vol!r} "
                "(volume mount not writable on this host).",
                file=sys.stderr,
            )
            rc = rsync_push_via_container(
                env,
                vol,
                source,
                delete=delete,
                dry_run=dry_run,
                alpine_image=alpine_image,
            )

    if rc != 0 and not dry_run:
        print(
            "bkp_files push: rsync failed; retry with `--method tar` for a full archive load.",
            file=sys.stderr,
        )
    return rc
