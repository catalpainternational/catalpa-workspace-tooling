"""Pull ``django_media`` Docker volume to a local directory (docker run + tar over DOCKER_HOST)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.restic_files import _default_compose_project, django_media_volume_name


def _local_docker_process_env() -> dict[str, str]:
    """Process env for ``docker run`` that must bind-mount **this machine's** paths.

    Inherited ``DOCKER_HOST`` may point at a remote engine (e.g. SSH during ``dk transfer``); clearing
    it makes the CLI use the default local daemon so ``-v /host/path:…`` resolves correctly.
    """
    out = os.environ.copy()
    out.pop("DOCKER_HOST", None)
    return out


def run_pull_media(
    env: dict[str, str],
    *,
    target: Path,
    dry_run: bool,
    alpine_image: str = "alpine:3.21",
    config: ProjectConfig | None = None,
) -> int:
    """Stream the named ``django_media`` volume to ``target`` using Linux ``tar`` in Docker end-to-end.

    Volume side uses ``DOCKER_HOST`` (e.g. SSH). Extract-to-disk uses the **local** Docker daemon
    with a bind mount so archives are unpacked by the same Alpine ``tar`` family as deploy hosts,
    avoiding macOS host ``tar`` incompatibility with streamed payloads.
    """
    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip() or _default_compose_project(config)
    vol = django_media_volume_name(project, config=config)
    run_env = os.environ.copy()
    for k, v in env.items():
        if v is not None:
            run_env[k] = str(v)

    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "-v",
        f"{vol}:/data:ro",
        alpine_image,
        "tar",
        "c",
        "-C",
        "/data",
        ".",
    ]
    extract_cmd = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--platform",
        "linux/amd64",
        "-v",
        f"{target.resolve()}:/out",
        alpine_image,
        "tar",
        "x",
        "-C",
        "/out",
    ]
    if dry_run:
        print(
            f"dry-run: extract volume {vol!r} -> {target.resolve()}",
            file=sys.stderr,
        )
        print(
            f"dry-run: {' '.join(docker_cmd)} | {' '.join(extract_cmd)}",
            file=sys.stderr,
        )
        return 0

    target.mkdir(parents=True, exist_ok=True)

    p1 = subprocess.Popen(
        docker_cmd,
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert p1.stdout is not None
    assert p1.stderr is not None
    local_env = _local_docker_process_env()
    try:
        tar_proc = subprocess.run(
            extract_cmd,
            stdin=p1.stdout,
            stderr=subprocess.PIPE,
            env=local_env,
            check=False,
        )
    finally:
        p1.stdout.close()
    rc_docker = p1.wait()
    err_docker = p1.stderr.read()
    if rc_docker != 0:
        print(
            f"docker run failed (exit {rc_docker}) for volume {vol!r}.",
            file=sys.stderr,
        )
        if err_docker:
            sys.stderr.buffer.write(err_docker)
        return rc_docker
    if tar_proc.returncode != 0:
        print(
            f"extract docker run failed (exit {tar_proc.returncode}) into {target}.",
            file=sys.stderr,
        )
        if tar_proc.stderr:
            sys.stderr.buffer.write(tar_proc.stderr)
        return tar_proc.returncode
    return 0


def run_push_media(
    env: dict[str, str],
    *,
    source: Path,
    dry_run: bool,
    alpine_image: str = "alpine:3.21",
    config: ProjectConfig | None = None,
) -> int:
    """Stream a local directory into the named ``django_media`` volume (``tar`` | ``docker run``).

    Clears existing volume top-level entries first (``find … -delete``), then extracts the archive
    from stdin. The archive is produced by **Linux** ``tar`` inside Docker (not the host ``tar``),
    so macOS BSD tar quirks do not drop files when unpacking on Alpine. Uses the same
    ``DOCKER_HOST`` / ``COMPOSE_PROJECT_NAME`` resolution as ``run_pull_media`` for the destination
    volume container; packing uses the **local** Docker daemon so bind mounts refer to this machine.
    """
    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip() or _default_compose_project(config)
    vol = django_media_volume_name(project, config=config)
    run_env = os.environ.copy()
    for k, v in env.items():
        if v is not None:
            run_env[k] = str(v)

    if not source.is_dir():
        print(f"push_media: not a directory: {source}", file=sys.stderr)
        return 1

    # Clear volume contents then extract from stdin (POSIX ``find`` in alpine).
    inner = r"find /data -mindepth 1 -delete && tar x -C /data"
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--platform",
        "linux/amd64",
        "-v",
        f"{vol}:/data",
        alpine_image,
        "sh",
        "-c",
        inner,
    ]
    pack_cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "-v",
        f"{source.resolve()}:/src:ro",
        alpine_image,
        "tar",
        "c",
        "-C",
        "/src",
        ".",
    ]
    if dry_run:
        print(
            f"dry-run: pack {source.resolve()} -> volume {vol!r}",
            file=sys.stderr,
        )
        print(f"dry-run: {' '.join(pack_cmd)} | {' '.join(docker_cmd)}", file=sys.stderr)
        return 0

    local_env = _local_docker_process_env()
    tar_proc = subprocess.Popen(
        pack_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=local_env,
    )
    assert tar_proc.stdout is not None
    assert tar_proc.stderr is not None
    try:
        docker_proc = subprocess.Popen(
            docker_cmd,
            env=run_env,
            stdin=tar_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    finally:
        tar_proc.stdout.close()

    assert docker_proc.stdout is not None
    assert docker_proc.stderr is not None
    out_d, err_d = docker_proc.communicate()
    rc_tar = tar_proc.wait()
    err_tar = tar_proc.stderr.read()

    if rc_tar != 0:
        print(f"tar create failed (exit {rc_tar}) from {source}.", file=sys.stderr)
        if err_tar:
            sys.stderr.buffer.write(err_tar)
        docker_proc.wait()
        return rc_tar
    if docker_proc.returncode != 0:
        print(
            f"docker run failed (exit {docker_proc.returncode}) for volume {vol!r}.",
            file=sys.stderr,
        )
        if err_d:
            sys.stderr.buffer.write(err_d)
        if out_d:
            sys.stderr.buffer.write(out_d)
        return docker_proc.returncode or 1
    return 0
