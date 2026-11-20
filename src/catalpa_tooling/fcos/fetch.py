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
from dataclasses import dataclass
from pathlib import Path

import yaml

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.fcos.host import Committed, Host, load_committed
from catalpa_tooling.fcos.render import generated_dir
from catalpa_tooling.fcos.settings import FcosConfigError, runtime_of


def _fail(message: str) -> SystemExit:
    print(message, file=sys.stderr)
    return SystemExit(1)


@dataclass(frozen=True)
class QuadletEnv:
    env_name: str
    committed: Committed
    host: Host
    login: str

    @property
    def ssh_target(self) -> str:
        return f"{self.login}@{self.host.address}"


def quadlet_env(config: ProjectConfig, env_name: str) -> QuadletEnv | None:
    """The env's host and committed render when it has ``runtime: quadlet``, else None."""
    from catalpa_tooling.fcos.host import host_from_docker_host
    from catalpa_tooling.ssh_known_hosts import ensure_known_host
    from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

    info_path = config.deploy_envs_dir / env_name / "info.yaml"
    if not info_path.is_file():
        return None
    info = yaml.safe_load(info_path.read_text(encoding="utf-8")) or {}
    try:
        if runtime_of(info) != "quadlet":
            return None
    except FcosConfigError:
        return None
    docker_host = str(info.get("docker_host") or "")
    committed = load_committed(generated_dir(config, env_name))
    host = host_from_docker_host(docker_host, None)
    login = parse_docker_host_to_ssh_target(docker_host).split("@", 1)[0]
    if ensure_known_host(host.address, port=host.port, recovery_env_name=env_name) != 0:
        raise SystemExit(1)
    return QuadletEnv(env_name=env_name, committed=committed, host=host, login=login)


def pg_dump_command(db_service: str, extra_args: Sequence[str] = ()) -> str:
    """The remote command that writes the app database as a ``-Fc`` archive to stdout."""
    from catalpa_tooling.pgbackrest_db import _DJANGO_APP_SHELL_VARS

    inner = _DJANGO_APP_SHELL_VARS + 'exec pg_dump -U postgres -d "$APP_DB" -Fc'
    if extra_args:
        inner += " " + " ".join(shlex.quote(a) for a in extra_args)
    return shlex.join(["podman", "exec", "-i", "-u", "postgres", db_service, "sh", "-c", inner])


def fetch_db(
    config: ProjectConfig,
    q: QuadletEnv,
    *,
    dest: Path,
    pg_dump_args: Sequence[str] | None = None,
    popen=subprocess.Popen,
) -> None:
    from catalpa_tooling.fetch_db import _atomic_write_from_stream

    db = config.stack.services.db
    if db not in (q.committed.manifest.get("images") or {}):
        raise _fail(f"fetch db: {q.env_name}'s render has no {db!r} service")
    command = pg_dump_command(db, pg_dump_args or ())
    print(f"Fetching the app database via ssh {q.ssh_target} (podman exec {db} pg_dump -Fc) → {dest}", file=sys.stderr)
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


def media_path(config: ProjectConfig, q: QuadletEnv) -> str:
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
