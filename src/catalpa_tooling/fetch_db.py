"""Config-driven PostgreSQL dump fetch (``dk fetch db``)."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from catalpa_tooling.config import FetchDatabaseEntry, ProjectConfig
from catalpa_tooling.deprecation import warn_deprecated
from catalpa_tooling.managed_deploy_env import load_managed_deploy_context
from catalpa_tooling.media_rsync import ssh_target_from_host
from catalpa_tooling.pgbackrest_db import (
    _PG_DUMP_CUSTOM_MAGIC,
    db_service_responds,
    ensure_db_service_running,
    run_pg_dump_to_file,
)
from catalpa_tooling.ssh_known_hosts import ensure_ssh_known_host_for_ssh_target

_MIN_CUSTOM_DUMP_BYTES = 100_000


def _ensure_ssh(ssh_host: str) -> None:
    target = ssh_target_from_host(ssh_host)
    if ensure_ssh_known_host_for_ssh_target(target) != 0:
        raise SystemExit(1)


def _atomic_write_from_stream(dest: Path, stream) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with open(tmp, "wb") as out_f:
            shutil.copyfileobj(stream, out_f)
        if tmp.stat().st_size < _MIN_CUSTOM_DUMP_BYTES:
            raise ValueError(
                f"dump at {dest} is only {tmp.stat().st_size} bytes "
                f"(expected at least {_MIN_CUSTOM_DUMP_BYTES})"
            )
        with open(tmp, "rb") as f:
            if f.read(5) != _PG_DUMP_CUSTOM_MAGIC:
                raise ValueError(f"{dest} does not look like a pg_dump -Fc archive")
        tmp.replace(dest)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _fetch_ssh_native(*, ssh_host: str, db_name: str, dest: Path) -> None:
    _ensure_ssh(ssh_host)
    target = ssh_target_from_host(ssh_host)
    remote_cmd = f"sudo -u postgres pg_dump -d {shlex.quote(db_name)} -Fc"
    print(f"Fetching {db_name} via ssh {target} (pg_dump -Fc) → {dest}", file=sys.stderr)
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target, remote_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        if proc.stderr:
            sys.stderr.buffer.write(proc.stderr)
        raise SystemExit(proc.returncode)
    import io

    _atomic_write_from_stream(dest, io.BytesIO(proc.stdout))


def _fetch_ssh_docker(
    *,
    ssh_host: str,
    container: str,
    db_name: str,
    pg_user: str,
    dest: Path,
) -> None:
    _ensure_ssh(ssh_host)
    target = ssh_target_from_host(ssh_host)
    remote_cmd = (
        f"docker exec {shlex.quote(container)} "
        f"pg_dump -U {shlex.quote(pg_user)} -d {shlex.quote(db_name)} -Fc"
    )
    print(
        f"Fetching {db_name} via ssh {target} (docker exec {container} pg_dump -Fc) → {dest}",
        file=sys.stderr,
    )
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", target, remote_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        if proc.stderr:
            sys.stderr.buffer.write(proc.stderr)
        raise SystemExit(proc.returncode)
    import io

    _atomic_write_from_stream(dest, io.BytesIO(proc.stdout))


def _fetch_via_dk(
    config: ProjectConfig,
    *,
    dk_env: str,
    db_name: str,
    dest: Path,
    pg_dump_args: Sequence[str] | None,
) -> None:
    ctx = load_managed_deploy_context(config, dk_env)
    if ctx is None:
        raise SystemExit(1)
    compose_file = ctx.compose_file
    env_add = ctx.env_add
    if not db_service_responds(compose_file, env_add):
        rc = ensure_db_service_running(
            compose_file,
            env_add,
            config=config,
            dk_env_name=dk_env,
        )
        if rc != 0:
            raise SystemExit(rc)
    print(f"Fetching {db_name} via dk {dk_env} bkp_db pgdump → {dest}", file=sys.stderr)
    rc = run_pg_dump_to_file(compose_file, env_add, dest, pg_dump_args)
    if rc != 0:
        raise SystemExit(rc)
    if dest.stat().st_size < _MIN_CUSTOM_DUMP_BYTES:
        print(
            f"fetch db: dump at {dest} is only {dest.stat().st_size} bytes "
            f"(expected at least {_MIN_CUSTOM_DUMP_BYTES}).",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _fetch_via_script(config: ProjectConfig, *, dest: Path, dk_env: str) -> None:
    warn_deprecated(
        "scripts/fetch_db.sh",
        "dk fetch db",
        context="configure native.fetch.databases in tooling.yaml",
    )
    script = config.scripts_dir / "fetch_db.sh"
    env = os.environ.copy()
    env["FETCH_DK_ENV"] = dk_env
    env["FETCH_DB_OUTPUT"] = str(dest)
    ssh_host = config.native.fetch.ssh_host
    if ssh_host:
        env["FETCH_DB_SSH_HOST"] = ssh_host
        _ensure_ssh(ssh_host)
    print(f"Running {script}", file=sys.stderr)
    subprocess.run(["bash", str(script)], cwd=config.repo_root, env=env, check=True)


def _resolve_ssh_host(config: ProjectConfig, entry: FetchDatabaseEntry) -> str:
    host = (entry.ssh_host or config.native.fetch.ssh_host or "").strip()
    if not host:
        raise ValueError(
            "fetch db requires ssh_host on the database entry or native.fetch.ssh_host in tooling.yaml"
        )
    return host


def run_fetch_database(
    config: ProjectConfig,
    key: str,
    entry: FetchDatabaseEntry,
    *,
    dest: Path,
    default_dk_env: str,
    pg_dump_args: Sequence[str] | None = None,
) -> None:
    """Fetch one configured database to ``dest``."""
    if not shutil.which("ssh") and entry.via in ("ssh_native", "ssh_docker"):
        print("ssh is not installed or not on PATH.", file=sys.stderr)
        raise SystemExit(1)

    dk_env = entry.dk_env or default_dk_env
    if entry.via == "ssh_native":
        _fetch_ssh_native(ssh_host=_resolve_ssh_host(config, entry), db_name=entry.db_name, dest=dest)
    elif entry.via == "ssh_docker":
        if not entry.container:
            raise ValueError(f"native.fetch.databases.{key}.container is required for ssh_docker")
        _fetch_ssh_docker(
            ssh_host=_resolve_ssh_host(config, entry),
            container=entry.container,
            db_name=entry.db_name,
            pg_user=entry.pg_user,
            dest=dest,
        )
    elif entry.via == "dk":
        _fetch_via_dk(
            config,
            dk_env=dk_env,
            db_name=entry.db_name,
            dest=dest,
            pg_dump_args=pg_dump_args,
        )
    elif entry.via == "script":
        _fetch_via_script(config, dest=dest, dk_env=dk_env)
    else:
        raise ValueError(f"unsupported fetch via: {entry.via!r}")

    print(f"Wrote {dest}", file=sys.stderr)


def run_fetch_all_dbs(
    config: ProjectConfig,
    *,
    dk_env: str | None = None,
    only: str | None = None,
    output_overrides: dict[str, Path] | None = None,
    pg_dump_args: Sequence[str] | None = None,
) -> None:
    """Fetch all (or one) configured databases to their ``paths.fetch_*_dump`` targets."""
    fetch = config.native.fetch
    env_name = dk_env if dk_env is not None else fetch.dk_env
    overrides = output_overrides or {}
    keys = [only] if only else list(fetch.databases.keys())
    if only and only not in fetch.databases:
        print(f"fetch db: no database configured for key {only!r}", file=sys.stderr)
        raise SystemExit(1)

    for key in keys:
        entry = fetch.databases[key]
        dest = overrides.get(key)
        if dest is None:
            resolved = config.fetch_database_output_path(key, entry)
            if resolved is None:
                print(
                    f"fetch db: skipping {key!r} — no output path in tooling.yaml "
                    f"(set paths.fetch_{key}_db_dump or databases.{key}.dump)",
                    file=sys.stderr,
                )
                continue
            dest = resolved
        dest = dest.expanduser().resolve()
        run_fetch_database(
            config,
            key,
            entry,
            dest=dest,
            default_dk_env=env_name,
            pg_dump_args=pg_dump_args,
        )


def dump_path_usable(path: Path) -> bool:
    """True when ``path`` exists, is non-trivial size, and looks like a custom-format dump."""
    if not path.is_file():
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < _MIN_CUSTOM_DUMP_BYTES:
        return False
    try:
        with open(path, "rb") as f:
            return f.read(5) == _PG_DUMP_CUSTOM_MAGIC
    except OSError:
        return False


def configured_app_dump_exists(config: ProjectConfig) -> bool:
    return dump_path_usable(config.fetch_db_dump_path)


def configured_metabase_dump_exists(config: ProjectConfig) -> bool:
    path = config.fetch_metabase_db_dump_path
    return path is not None and dump_path_usable(path)
