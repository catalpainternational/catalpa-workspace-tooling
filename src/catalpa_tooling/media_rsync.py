"""Rsync helpers for ``django_media`` volume sync (fetch + ``bkp_files push`` + transfer)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.media_storage import MediaStorage, MediaStorageKind
from catalpa_tooling.restic_files import _default_compose_project, django_media_volume_name
from catalpa_tooling.run_cmd import format_shell_command, run as run_cmd
from catalpa_tooling.ssh_known_hosts import (
    is_ssh_host_key_verification_error,
    print_ssh_host_key_hint,
)
from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

if TYPE_CHECKING:
    from catalpa_tooling.config import ProjectConfig

# Like ``rsync -az`` but omit ``-l`` (--links): symbolic links on the server are skipped.
RSYNC_BASE: tuple[str, ...] = ("rsync", "-rptgoDzv", "--progress")

PushMediaMethod = Literal["rsync", "tar"]
TransferMediaMethod = Literal["rsync", "tar"]


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


def _parse_volume_mountpoint_json(
    stdout: str,
    volume_name: str,
    *,
    label: str,
    soft: bool = False,
) -> str | None:
    try:
        payload = json.loads(stdout or "[]")
    except json.JSONDecodeError as exc:
        print(
            f"{label}: invalid JSON from docker volume inspect for {volume_name!r}: {exc}",
            file=sys.stderr,
        )
        if soft:
            return None
        raise SystemExit(1) from exc
    if not isinstance(payload, list) or not payload:
        print(f"{label}: volume {volume_name!r} not found.", file=sys.stderr)
        if soft:
            return None
        raise SystemExit(1)
    first = payload[0]
    if not isinstance(first, dict):
        print(f"{label}: unexpected inspect payload for {volume_name!r}.", file=sys.stderr)
        if soft:
            return None
        raise SystemExit(1)
    mount = str(first.get("Mountpoint") or "").strip()
    if not mount:
        print(
            f"{label}: empty Mountpoint from docker volume inspect for {volume_name!r}",
            file=sys.stderr,
        )
        if soft:
            return None
        raise SystemExit(1)
    return mount.rstrip("/")


def docker_volume_mountpoint_ssh(ssh_target: str, volume_name: str, *, label: str = "media") -> str:
    """``docker volume inspect`` on a remote host via SSH; return mount path."""
    mount = try_docker_volume_mountpoint_ssh(ssh_target, volume_name, label=label)
    if mount is None:
        raise SystemExit(1)
    return mount


def try_docker_volume_mountpoint_ssh(
    ssh_target: str,
    volume_name: str,
    *,
    label: str = "media",
) -> str | None:
    """Like ``docker_volume_mountpoint_ssh`` but return ``None`` on failure (no ``SystemExit``)."""
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
        return None
    return _parse_volume_mountpoint_json(
        proc.stdout or "", volume_name, label=label, soft=True
    )


def docker_volume_mountpoint_local(
    env: dict[str, str],
    volume_name: str,
    *,
    label: str = "media",
) -> str:
    """``docker volume inspect`` using ``DOCKER_HOST`` from ``env``; return mount path."""
    mount = try_docker_volume_mountpoint_local(env, volume_name, label=label)
    if mount is None:
        raise SystemExit(1)
    return mount


def try_docker_volume_mountpoint_local(
    env: dict[str, str],
    volume_name: str,
    *,
    label: str = "media",
) -> str | None:
    """Like ``docker_volume_mountpoint_local`` but return ``None`` on failure (no ``SystemExit``)."""
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
        return None
    return _parse_volume_mountpoint_json(
        proc.stdout or "", volume_name, label=label, soft=True
    )


def mountpoint_host_rsync_writable(mount: str) -> bool:
    """True when the host can rsync directly into the volume mount (typical Linux, not macOS VM paths)."""
    if sys.platform == "darwin":
        return False
    p = Path(mount)
    try:
        return p.is_dir() and os.access(p, os.W_OK)
    except OSError:
        return False


def resolve_rsync_endpoint(
    env: dict[str, str],
    storage: MediaStorage,
    *,
    label: str = "media",
    config: ProjectConfig | None = None,
) -> str | None:
    """Return a host-reachable rsync location, or ``None`` when staging is required.

    Formats:
    - local path: ``/var/lib/docker/volumes/…/_data`` or a bind directory
    - remote: ``user@host:/var/lib/docker/volumes/…/_data``
    """
    del config  # reserved for future volume-name overrides
    if storage.kind is MediaStorageKind.BIND:
        host = Path(storage.location)
        if host.is_dir():
            return str(host.resolve())
        print(f"{label}: bind path is not a directory: {host}", file=sys.stderr)
        return None

    docker_host = str(env.get("DOCKER_HOST") or "").strip()
    ssh_target = try_ssh_target_from_docker_host(docker_host)
    if ssh_target:
        mount = try_docker_volume_mountpoint_ssh(
            ssh_target, storage.location, label=label
        )
        if mount is None:
            return None
        return f"{ssh_target}:{mount}"

    mount = try_docker_volume_mountpoint_local(env, storage.location, label=label)
    if mount is None:
        return None
    if mountpoint_host_rsync_writable(mount):
        return mount
    return None


def _normalize_rsync_src(src: str) -> str:
    """Ensure trailing slash so rsync copies directory *contents*."""
    if src.endswith(":"):
        return src
    # Remote ``user@host:path`` — slash after path, not after host.
    if "@" in src and ":" in src.split("@", 1)[-1]:
        if not src.endswith("/"):
            return src + "/"
        return src
    if not src.endswith(os.sep) and not src.endswith("/"):
        return src + "/"
    return src


def _normalize_rsync_dest(dest: str) -> str:
    if not dest.endswith("/"):
        return dest + "/"
    return dest


def rsync_between_endpoints(
    src: str,
    dst: str,
    *,
    delete: bool,
    dry_run: bool,
) -> int:
    """Rsync ``src/`` to ``dst/`` (local paths and/or ``user@host:path``)."""
    src_norm = _normalize_rsync_src(src)
    dest_norm = _normalize_rsync_dest(dst)
    cmd: list[str] = list(RSYNC_BASE)
    if delete:
        cmd.append("--delete")
    if dry_run:
        cmd.extend(["--dry-run", "--itemize-changes"])
    cmd.extend([src_norm, dest_norm])
    print(f"$ {format_shell_command(cmd)}", file=sys.stderr)
    return run_cmd(cmd, check=False, print_cmd=False).returncode


def rsync_pull_remote_to_local(ssh_target: str, remote_path: str, local_path: Path) -> int:
    """Rsync from ``ssh:remote_path`` into ``local_path`` (``dev fetch media``)."""
    local_path.mkdir(parents=True, exist_ok=True)
    remote = f"{ssh_target}:{remote_path}"
    return rsync_between_endpoints(remote, str(local_path), delete=False, dry_run=False)


def rsync_push_local_to_dest(
    source: Path,
    dest: str,
    *,
    delete: bool,
    dry_run: bool,
) -> int:
    """Rsync ``source/`` to ``dest`` (local path or ``user@host:path``)."""
    return rsync_between_endpoints(str(source.resolve()), dest, delete=delete, dry_run=dry_run)


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
    config: ProjectConfig | None = None,
    storage: MediaStorage | None = None,
    label: str = "bkp_files push",
) -> int:
    """Push host media into ``django_media`` via rsync (tar fallback on failure or ``method=tar``)."""
    from catalpa_tooling.media_pull import run_push_media

    if method == "tar":
        return run_push_media(
            env,
            source=source,
            dry_run=dry_run,
            alpine_image=alpine_image,
            config=config,
            storage=storage,
        )

    if not shutil.which("rsync"):
        print(
            f"{label}: rsync is not on PATH; use `--method tar` or install rsync.",
            file=sys.stderr,
        )
        return 1

    if storage is not None and storage.kind is MediaStorageKind.BIND:
        dest = str(Path(storage.location).resolve())
        print(f"{label}: rsync → bind {dest}/", file=sys.stderr)
        rc = rsync_push_local_to_dest(source, dest + "/", delete=delete, dry_run=dry_run)
        if rc != 0 and not dry_run:
            print(
                f"{label}: rsync failed; retry with `--method tar` for a full archive load.",
                file=sys.stderr,
            )
        return rc

    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip() or _default_compose_project(config)
    if storage is not None and storage.kind is MediaStorageKind.VOLUME:
        vol = storage.location
    else:
        vol = django_media_volume_name(project, config=config)
    docker_host = str(env.get("DOCKER_HOST") or "").strip()

    ssh_target = try_ssh_target_from_docker_host(docker_host)
    if ssh_target:
        print(f"Resolving Docker volume {vol!r} on {ssh_target} …", file=sys.stderr)
        mount = docker_volume_mountpoint_ssh(ssh_target, vol, label=label)
        dest = f"{ssh_target}:{mount}/"
        rc = rsync_push_local_to_dest(source, dest, delete=delete, dry_run=dry_run)
    else:
        mount = docker_volume_mountpoint_local(env, vol, label=label)
        if mountpoint_host_rsync_writable(mount):
            print(f"{label}: rsync → {mount}/", file=sys.stderr)
            rc = rsync_push_local_to_dest(source, mount + "/", delete=delete, dry_run=dry_run)
        else:
            print(
                f"{label}: using container rsync into volume {vol!r} "
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
            f"{label}: rsync failed; retry with `--method tar` for a full archive load.",
            file=sys.stderr,
        )
    return rc
