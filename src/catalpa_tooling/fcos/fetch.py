"""``dk fetch db`` and ``dk fetch media`` from an env with ``runtime: quadlet``.

Compose doesn't run these stacks, so the compose path (``docker compose exec db pg_dump``,
then the media volume's mountpoint from ``docker volume inspect``) finds nothing. Both go over
SSH as the account in ``docker_host`` instead, from the committed ``generated/fcos/`` only:

- **db:** ``podman exec -u postgres <db> pg_dump -Fc`` over the container's local socket, so
  no password is needed. The database is the one compose's path dumps
  (``DJANGO_APP_DB``/``DJANGO_DB`` in the container's settings). The dump streams to a
  temporary file and replaces the destination only once it looks like a whole archive.
- **media:** rsync from the media volume's directory on the data volume (the manifest's
  ``bind_volumes``). Its directories are 0755, so the account can read it without sudo.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from catalpa_tooling.config import ProjectConfig

if TYPE_CHECKING:
    from catalpa_tooling.fcos.backend import Backend


def _fail(message: str) -> SystemExit:
    print(message, file=sys.stderr)
    return SystemExit(1)


def quadlet_env(config: ProjectConfig, env_name: str) -> Backend | None:
    """The env's quadlet ``Backend`` when it has ``runtime: quadlet``, else None. Raises
    ``ValueError`` when the env's render or host can't be loaded."""
    from catalpa_tooling.fcos.backend import EnvError, quadlet_backend

    try:
        return quadlet_backend(config, env_name)
    except EnvError as exc:
        raise ValueError(f"{env_name}: {exc}") from None


def pg_dump_command(db_service: str, extra_args: Sequence[str] = (), *, database: str | None = None) -> str:
    """The remote command that writes a database as a ``-Fc`` archive to stdout: ``database``, or
    the app database as the container resolves it (``$APP_DB``) when None, as compose does."""
    from catalpa_tooling.pgbackrest_db import _DJANGO_APP_SHELL_VARS

    if database is None:
        inner = _DJANGO_APP_SHELL_VARS + 'exec pg_dump -U postgres -d "$APP_DB" -Fc'
    else:
        inner = "exec pg_dump -U postgres -d " + shlex.quote(database) + " -Fc"
    if extra_args:
        inner += " " + " ".join(shlex.quote(a) for a in extra_args)
    return shlex.join(["podman", "exec", "-i", "-u", "postgres", db_service, "sh", "-c", inner])


def fetch_db(
    config: ProjectConfig,
    q: Backend,
    *,
    dest: Path,
    pg_dump_args: Sequence[str] | None = None,
    database: str | None = None,
    popen=subprocess.Popen,
) -> None:
    from catalpa_tooling.fetch_db import _atomic_write_from_stream

    db = config.stack.services.db
    if db not in (q.committed.manifest.get("images") or {}):
        raise _fail(f"fetch db: {q.env_name}'s render has no {db!r} service")
    command = pg_dump_command(db, pg_dump_args or (), database=database)
    label = "the app database" if database is None else database
    print(f"Fetching {label} via ssh {q.ssh_target} (podman exec {db} pg_dump -Fc) → {dest}", file=sys.stderr)
    argv = q.host._argv(q.login, command)
    proc = popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _atomic_write_from_stream(dest, proc.stdout)
    except ValueError:
        proc.wait()
        err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace").strip()
        raise _fail(f"fetch db: pg_dump on {q.ssh_target} failed: {err or 'no output'}") from None
    finally:
        if proc.stdout:
            proc.stdout.close()
    rc = proc.wait()
    if rc != 0:
        err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace").strip()
        dest.unlink(missing_ok=True)
        raise _fail(f"fetch db: pg_dump on {q.ssh_target} exited {rc}: {err}")


def media_path(config: ProjectConfig, q: Backend) -> str:
    """The media volume's directory on the host."""
    from catalpa_tooling.restic_files import restic_data_volume_key

    key = restic_data_volume_key(config)
    vols = q.committed.manifest.get("bind_volumes") or {}
    if key not in vols:
        raise ValueError(
            f"{q.env_name}: media volume {key!r} isn't kept on the data volume "
            f"(fcos.bind_volumes: {', '.join(sorted(vols)) or 'none'}), so there's no host "
            "directory to rsync"
        )
    return str(vols[key]["path"]).rstrip("/")
