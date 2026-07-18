"""Pull/push Django media (named Docker volume or host bind) via docker run + tar."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.media_storage import (
    MediaStorage,
    MediaStorageError,
    MediaStorageKind,
    resolve_media_storage,
)
from catalpa_tooling.restic_files import _default_compose_project, django_media_volume_name


def _local_docker_process_env() -> dict[str, str]:
    """Process env for ``docker run`` that must bind-mount **this machine's** paths.

    Inherited ``DOCKER_HOST`` may point at a remote engine (e.g. SSH during ``dk transfer``); clearing
    it makes the CLI use the default local daemon so ``-v /host/path:…`` resolves correctly.
    """
    out = os.environ.copy()
    out.pop("DOCKER_HOST", None)
    return out


def _merge_run_env(env: dict[str, str]) -> dict[str, str]:
    run_env = os.environ.copy()
    for k, v in env.items():
        if v is not None:
            run_env[k] = str(v)
    return run_env


def _resolve_storage_for_io(
    env: dict[str, str],
    *,
    config: ProjectConfig | None,
    compose_file: str | None,
    storage: MediaStorage | None,
) -> MediaStorage:
    if storage is not None:
        return storage
    if compose_file and config is not None:
        return resolve_media_storage(
            compose_file=compose_file,
            env=env,
            config=config,
        )
    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip() or _default_compose_project(config)
    return MediaStorage(
        MediaStorageKind.VOLUME,
        django_media_volume_name(project, config=config),
    )


def _docker_data_mount(storage: MediaStorage, *, read_only: bool) -> str:
    """``-v`` argument mounting storage at ``/data`` inside the helper container."""
    suffix = ":ro" if read_only else ""
    if storage.kind is MediaStorageKind.BIND:
        return f"{storage.location}:/data{suffix}"
    return f"{storage.location}:/data{suffix}"


def _source_env_for_storage(storage: MediaStorage, run_env: dict[str, str]) -> dict[str, str]:
    """Volume side honors DOCKER_HOST; bind mounts always use the local daemon."""
    if storage.kind is MediaStorageKind.BIND:
        return _local_docker_process_env()
    return run_env


def run_pull_media(
    env: dict[str, str],
    *,
    target: Path,
    dry_run: bool,
    alpine_image: str = "alpine:3.21",
    config: ProjectConfig | None = None,
    compose_file: str | None = None,
    storage: MediaStorage | None = None,
) -> int:
    """Stream media storage to ``target`` using Linux ``tar`` in Docker end-to-end.

    Named volumes use ``DOCKER_HOST`` (e.g. SSH). Host binds and extract-to-disk use the
    **local** Docker daemon with bind mounts so archives are handled by Alpine ``tar``.
    """
    try:
        resolved = _resolve_storage_for_io(
            env, config=config, compose_file=compose_file, storage=storage
        )
    except MediaStorageError as exc:
        print(f"pull_media: {exc}", file=sys.stderr)
        return 1

    run_env = _merge_run_env(env)
    source_env = _source_env_for_storage(resolved, run_env)
    data_mount = _docker_data_mount(resolved, read_only=True)

    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "-v",
        data_mount,
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
            f"dry-run: extract {resolved.describe()} -> {target.resolve()}",
            file=sys.stderr,
        )
        print(
            f"dry-run: {' '.join(docker_cmd)} | {' '.join(extract_cmd)}",
            file=sys.stderr,
        )
        return 0

    if resolved.kind is MediaStorageKind.BIND:
        host = Path(resolved.location)
        if not host.is_dir():
            print(f"pull_media: bind path is not a directory: {host}", file=sys.stderr)
            return 1

    target.mkdir(parents=True, exist_ok=True)

    p1 = subprocess.Popen(
        docker_cmd,
        env=source_env,
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
            f"docker run failed (exit {rc_docker}) for {resolved.describe()}.",
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
    compose_file: str | None = None,
    storage: MediaStorage | None = None,
) -> int:
    """Stream a local directory into media storage (``tar`` | ``docker run``).

    Clears existing top-level entries first (``find … -delete``), then extracts the archive
    from stdin. Named volumes honor ``DOCKER_HOST``; host binds use the local daemon.
    """
    try:
        resolved = _resolve_storage_for_io(
            env, config=config, compose_file=compose_file, storage=storage
        )
    except MediaStorageError as exc:
        print(f"push_media: {exc}", file=sys.stderr)
        return 1

    run_env = _merge_run_env(env)
    dest_env = _source_env_for_storage(resolved, run_env)
    data_mount = _docker_data_mount(resolved, read_only=False)

    if not source.is_dir():
        print(f"push_media: not a directory: {source}", file=sys.stderr)
        return 1

    # Clear volume/bind contents then extract from stdin (POSIX ``find`` in alpine).
    inner = r"find /data -mindepth 1 -delete && tar x -C /data"
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--platform",
        "linux/amd64",
        "-v",
        data_mount,
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
            f"dry-run: pack {source.resolve()} -> {resolved.describe()}",
            file=sys.stderr,
        )
        print(f"dry-run: {' '.join(pack_cmd)} | {' '.join(docker_cmd)}", file=sys.stderr)
        return 0

    if resolved.kind is MediaStorageKind.BIND:
        host = Path(resolved.location)
        host.mkdir(parents=True, exist_ok=True)

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
            env=dest_env,
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
            f"docker run failed (exit {docker_proc.returncode}) for {resolved.describe()}.",
            file=sys.stderr,
        )
        if err_d:
            sys.stderr.buffer.write(err_d)
        if out_d:
            sys.stderr.buffer.write(out_d)
        return docker_proc.returncode or 1
    return 0
