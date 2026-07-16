"""Deploy-time Postgres / pgBackRest named-volume config from PGBR_S3_* env (see README_PGBACKREST.md)."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

from catalpa_tooling.config import DEFAULT_PGBR_PG1_PATH, ProjectConfig
from catalpa_tooling.run_cmd import format_shell_command, run as run_cmd
from catalpa_tooling.storage_config import CADDY_DATA_VOLUME_KEY
from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

# PG 18+ data directory when compose mounts ``pgdata:/var/lib/postgresql`` (not …/data).
_PG18_PGDATA_RE = re.compile(r"^/var/lib/postgresql/\d+/")


def postgres_pg1_path(env: dict[str, str], *, config: ProjectConfig | None = None) -> str:
    """Cluster data directory path inside the ``db`` container (pgBackRest ``pg1-path``)."""
    explicit = (env.get("PGBR_PG1_PATH") or "").strip()
    if explicit:
        return explicit
    if config is not None:
        return config.ops.pgbackrest.pg1_path
    return DEFAULT_PGBR_PG1_PATH


def pgdata_volume_mount(pg1_path: str) -> str:
    """Docker ``-v`` mount target for the PGDATA named volume (must match ``compose.yml``)."""
    if _PG18_PGDATA_RE.match(pg1_path):
        return "/var/lib/postgresql"
    return pg1_path

PREFIX_WRITE = "PGBR_S3_WRITE_"
PREFIX_READ = "PGBR_S3_READ_"

# Suffixes after PGBR_S3_WRITE_ / PGBR_S3_READ_ (values map to pgBackRest repo1 options).
SUFFIX_TO_GLOBAL: dict[str, str] = {
    "BUCKET": "repo1-s3-bucket",
    "REGION": "repo1-s3-region",
    "ENDPOINT": "repo1-s3-endpoint",
    "KEY": "repo1-s3-key",
    "SECRET": "repo1-s3-key-secret",
    "REPO_PATH": "repo1-path",
    # Optional (Garage / MinIO / private CA): omit for Spaces/AWS defaults.
    "URI_STYLE": "repo1-s3-uri-style",
    "VERIFY_TLS": "repo1-storage-verify-tls",
}
REQUIRED_SUFFIXES = frozenset({"BUCKET", "REGION", "KEY", "SECRET", "REPO_PATH", "STANZA"})


@dataclass(frozen=True)
class PgbackrestRepoSettings:
    """S3 repo settings materialized into the pgbackrest_conf volume drop-in."""

    stanza: str
    repo_path: str
    bucket: str
    region: str
    endpoint: str
    pg1_path: str


def _parse_pgbackrest_managed_ini(content: str) -> PgbackrestRepoSettings | None:
    """Parse managed drop-in INI (``[global]`` + ``[<stanza>]`` with ``pg1-path``)."""
    global_opts: dict[str, str] = {}
    stanza_name = ""
    stanza_opts: dict[str, str] = {}
    section: str | None = None
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            if section != "global" and not section.startswith("global:"):
                stanza_name = section
                stanza_opts = {}
            continue
        if "=" not in line or section is None:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if section == "global":
            global_opts[key] = value
        elif section == stanza_name:
            stanza_opts[key] = value
    repo_path = global_opts.get("repo1-path", "")
    bucket = global_opts.get("repo1-s3-bucket", "")
    pg1_path = stanza_opts.get("pg1-path", "")
    if not stanza_name or not repo_path or not bucket or not pg1_path:
        return None
    return PgbackrestRepoSettings(
        stanza=stanza_name,
        repo_path=repo_path,
        bucket=bucket,
        region=global_opts.get("repo1-s3-region", ""),
        endpoint=global_opts.get("repo1-s3-endpoint", ""),
        pg1_path=pg1_path,
    )


def expected_pgbackrest_repo_settings(
    env: dict[str, str], *, config: ProjectConfig | None = None
) -> PgbackrestRepoSettings | None:
    """Repo settings that ``materialize_configs`` would write from current env."""
    mode = resolve_mode(env)
    if mode not in ("write", "read"):
        return None
    prefix = PREFIX_WRITE if mode == "write" else PREFIX_READ
    vars_map = _extract_stanza_vars(env, prefix)
    if _validate_repo_vars(vars_map, mode=mode):
        return None
    return PgbackrestRepoSettings(
        stanza=vars_map["STANZA"],
        repo_path=vars_map["REPO_PATH"],
        bucket=vars_map["BUCKET"],
        region=vars_map.get("REGION", ""),
        endpoint=vars_map.get("ENDPOINT", ""),
        pg1_path=postgres_pg1_path(env, config=config),
    )


def repo_settings_match(
    volume: PgbackrestRepoSettings, expected: PgbackrestRepoSettings
) -> bool:
    return (
        volume.stanza == expected.stanza
        and volume.repo_path == expected.repo_path
        and volume.bucket == expected.bucket
        and volume.region == expected.region
        and volume.endpoint == expected.endpoint
        and volume.pg1_path == expected.pg1_path
    )


def _require_pgbackrest_conf_name(config: ProjectConfig | None) -> str:
    return _require_pgbackrest_ops(config).pgbackrest_conf


def _require_pgbackrest_ops(config: ProjectConfig | None) -> PgbackrestOpsConfig:
    if config is None:
        from catalpa_tooling.config import ProjectConfigError

        raise ProjectConfigError("ProjectConfig is required for pgBackRest volume operations")
    return config.ops.pgbackrest


def read_managed_pgbackrest_repo_settings(
    env: dict[str, str], *, config: ProjectConfig | None = None
) -> PgbackrestRepoSettings | None:
    """Read and parse the managed pgBackRest drop-in from the ``pgbackrest_conf`` volume."""
    if not pgbackrest_managed_conf_materialized(env, config=config):
        return None
    conf_name = _require_pgbackrest_conf_name(config)
    vol_pgb = volume_names(env, config=config)[1]
    image = postgres_image_from_env(env, config=config)
    docker_env = _docker_env_for_remote(env)
    conf_path = f"/etc/pgbackrest/conf.d/{conf_name}"
    r = run_cmd(
        [
            "docker",
            "run",
            "--rm",
            *_compose_db_platform_args(),
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{vol_pgb}:/etc/pgbackrest/conf.d",
            image,
            "-c",
            f"cat {shlex.quote(conf_path)}",
        ],
        env=docker_env,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode != 0:
        return None
    return _parse_pgbackrest_managed_ini(r.stdout or "")


def describe_pgbackrest_conf_status(
    env: dict[str, str], *, config: ProjectConfig | None = None
) -> str:
    """Human-readable volume config vs credentials (for ``db restore --dry-run``)."""
    expected = expected_pgbackrest_repo_settings(env, config=config)
    if expected is None:
        mode = resolve_mode(env)
        if mode == "none":
            return "no PGBR_S3_* credentials (configure required before restore)"
        conflict = conflict_error_message(env)
        if conflict:
            return "credential conflict (WRITE and READ both set)"
        return "incomplete PGBR_S3_* credentials"
    volume = read_managed_pgbackrest_repo_settings(env, config=config)
    if volume is None:
        return "volume config missing (would run `db configure` before restore)"
    if repo_settings_match(volume, expected):
        return "volume config matches credentials"
    return (
        "volume config STALE — would re-run `db configure` before restore:\n"
        f"    volume:  stanza={volume.stanza!r} repo1-path={volume.repo_path!r} "
        f"bucket={volume.bucket!r}\n"
        f"    env:     stanza={expected.stanza!r} repo1-path={expected.repo_path!r} "
        f"bucket={expected.bucket!r}"
    )


# Optional tuning (env), not part of PGBR_S3_WRITE_/READ_ — see render_pgbackrest_ini / README_PGBACKREST.md.
def _env_str(env: dict[str, str], key: str, default: str) -> str:
    v = env.get(key)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


def _env_yn(env: dict[str, str], key: str, *, default: bool) -> str:
    """Return ``y`` or ``n`` for pgBackRest boolean options."""
    v = env.get(key)
    if v is None or str(v).strip() == "":
        return "y" if default else "n"
    low = str(v).strip().lower()
    if low in ("y", "yes", "1", "true", "on"):
        return "y"
    if low in ("n", "no", "0", "false", "off"):
        return "n"
    return "y" if default else "n"


def _compose_project_name(env: dict[str, str], config: ProjectConfig | None) -> str:
    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip()
    if project:
        return project
    if config is None:
        from catalpa_tooling.config import ProjectConfigError

        raise ProjectConfigError(
            "COMPOSE_PROJECT_NAME is unset; pass ProjectConfig or set COMPOSE_PROJECT_NAME in env"
        )
    return config.stack.compose_project_default


def postgres_image_from_env(env: dict[str, str], *, config: ProjectConfig | None = None) -> str:
    """Resolved db stack image ref (same defaults as ``compose.yml`` ``db`` service)."""
    if config is None:
        from catalpa_tooling.config import ProjectConfigError

        raise ProjectConfigError("ProjectConfig is required for postgres_image_from_env")
    explicit = (env.get("Postgres_IMAGE") or "").strip()
    if explicit:
        return explicit
    default_reg = config.ops.pgbackrest.default_registry
    reg = (env.get("STACK_IMAGE_REGISTRY") or default_reg).strip().rstrip("/") or default_reg
    tag = (env.get("STACK_IMAGE_TAG") or "latest").strip() or "latest"
    db_image = config.image_component("db")
    return f"{reg}/{db_image}:{tag}"


def volume_names(env: dict[str, str], *, config: ProjectConfig | None = None) -> tuple[str, str]:
    """Docker volume names for postgres_conf and pgbackrest_conf (same as compose.yml ``name:``)."""
    project = _compose_project_name(env, config)
    return (f"{project}_postgres_conf", f"{project}_pgbackrest_conf")


def _postgres_data_volume_key(config: ProjectConfig | None) -> str:
    if config is not None:
        return config.ops.pgbackrest.data_volume
    return "postgres_data"


def postgres_data_volume_name(env: dict[str, str], *, config: ProjectConfig | None = None) -> str:
    """Docker volume name for the compose PGDATA volume (``{project}_{data_volume}``)."""
    project = _compose_project_name(env, config)
    return f"{project}_{_postgres_data_volume_key(config)}"


def django_media_volume_name(env: dict[str, str], *, config: ProjectConfig | None = None) -> str:
    """Docker volume name for the restic backup target (``{project}_{ops.restic.data_volume}``)."""
    from catalpa_tooling.restic_files import restic_data_volume_key

    project = _compose_project_name(env, config)
    vol_key = restic_data_volume_key(config)
    return f"{project}_{vol_key}"


def caddy_data_volume_name(env: dict[str, str], *, config: ProjectConfig | None = None) -> str:
    """Docker volume name for ``caddy_data`` (same as compose.yml ``name:``)."""
    project = _compose_project_name(env, config)
    return f"{project}_caddy_data"


def stack_volume_docker_name(
    env: dict[str, str],
    volume_key: str,
    *,
    config: ProjectConfig | None = None,
) -> str:
    """Docker volume name for a bindable compose volume key."""
    if config is not None:
        if volume_key == config.ops.pgbackrest.data_volume:
            return postgres_data_volume_name(env, config=config)
        if volume_key == config.ops.restic.data_volume:
            return django_media_volume_name(env, config=config)
    if volume_key == CADDY_DATA_VOLUME_KEY:
        return caddy_data_volume_name(env, config=config)
    if volume_key == "postgres_data":
        return postgres_data_volume_name(env, config=config)
    if volume_key == "django_media":
        return django_media_volume_name(env, config=config)
    project = _compose_project_name(env, config)
    return f"{project}_{volume_key}"


_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
_COMPOSE_VOLUME_LABEL = "com.docker.compose.volume"


def _compose_volume_create_label_args(
    compose_project: str,
    compose_volume_key: str,
) -> list[str]:
    """Docker CLI args so Compose recognizes a pre-created named volume as its own."""
    project = compose_project.strip()
    key = compose_volume_key.strip()
    if not project or not key:
        return []
    return [
        "--label",
        f"{_COMPOSE_PROJECT_LABEL}={project}",
        "--label",
        f"{_COMPOSE_VOLUME_LABEL}={key}",
    ]


def _stack_volumes_with_compose_keys(
    env: dict[str, str], *, config: ProjectConfig | None = None
) -> tuple[tuple[str, str], ...]:
    """``(docker_volume_name, compose_volume_key)`` pairs for stack volume ensure."""
    pg_conf, pgb_conf = volume_names(env, config=config)
    pg_data_key = _postgres_data_volume_key(config)
    media_key = config.ops.restic.data_volume if config else "django_media"
    return (
        (postgres_data_volume_name(env, config=config), pg_data_key),
        (django_media_volume_name(env, config=config), media_key),
        (caddy_data_volume_name(env, config=config), CADDY_DATA_VOLUME_KEY),
        (pg_conf, "postgres_conf"),
        (pgb_conf, "pgbackrest_conf"),
    )


def _db_volumes_with_compose_keys(
    env: dict[str, str], *, config: ProjectConfig | None = None
) -> tuple[tuple[str, str], ...]:
    """``(docker_volume_name, compose_volume_key)`` for volumes mounted by ``db``."""
    pg_conf, pgb_conf = volume_names(env, config=config)
    pg_data_key = _postgres_data_volume_key(config)
    return (
        (postgres_data_volume_name(env, config=config), pg_data_key),
        (pg_conf, "postgres_conf"),
        (pgb_conf, "pgbackrest_conf"),
    )


def _compose_db_platform_args() -> list[str]:
    """Match ``compose.yml`` ``db`` service ``platform: linux/amd64`` for one-off ``docker run``."""
    return ["--platform", "linux/amd64"]


def _docker_run_s3_network_args(env: dict[str, str]) -> list[str]:
    """``--add-host`` / CA bind-mount for S3-reaching pgBackRest one-shots."""
    from catalpa_tooling.docker_host_tls import docker_add_host_args, docker_ca_volume_args

    return [*docker_add_host_args(env), *docker_ca_volume_args(env)]


def _pgdata_has_control_file(
    docker_env: dict[str, str],
    image: str,
    data_volume: str,
    *,
    pg1_path: str,
) -> bool:
    """True if the named volume contains an initialized cluster (``global/pg_control``)."""
    mount = pgdata_volume_mount(pg1_path)
    control = f"{pg1_path}/global/pg_control"
    r = run_cmd(
        [
            "docker",
            "run",
            "--rm",
            *_compose_db_platform_args(),
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{data_volume}:{mount}",
            image,
            "-c",
            f"test -f {shlex.quote(control)}",
        ],
        env=docker_env,
        capture_output=True,
        check=False,
        print_cmd=False,
    )
    return r.returncode == 0


def _nonempty_prefix_keys(env: dict[str, str], prefix: str) -> list[str]:
    out: list[str] = []
    for k, v in env.items():
        if not k.startswith(prefix):
            continue
        if str(v).strip():
            out.append(k)
    return sorted(out)


def resolve_mode(env: dict[str, str]) -> Literal["write", "read", "none", "both"]:
    write_keys = _nonempty_prefix_keys(env, PREFIX_WRITE)
    read_keys = _nonempty_prefix_keys(env, PREFIX_READ)
    if write_keys and read_keys:
        return "both"
    if write_keys:
        return "write"
    if read_keys:
        return "read"
    return "none"


def _extract_stanza_vars(env: dict[str, str], prefix: str) -> dict[str, str]:
    """Map known suffixes (e.g. BUCKET) to string values from PGBR_S3_* env."""
    plen = len(prefix)
    out: dict[str, str] = {}
    for k, v in env.items():
        if not k.startswith(prefix):
            continue
        suffix = k[plen:]
        if not suffix or suffix not in (
            set(SUFFIX_TO_GLOBAL) | {"STANZA"} | {"RETENTION_FULL"} | {"ENDPOINT"}
        ):
            continue
        out[suffix] = str(v).strip()
    return out


def _validate_repo_vars(vars_map: dict[str, str], *, mode: Literal["write", "read"]) -> str | None:
    missing = [s for s in REQUIRED_SUFFIXES if not vars_map.get(s)]
    if missing:
        pfx = PREFIX_WRITE if mode == "write" else PREFIX_READ
        need = ", ".join(f"{pfx}{s}" for s in missing)
        return f"pgBackRest: missing required env for {mode} mode: {need}"
    return None


def render_pgbackrest_ini(
    mode: Literal["write", "read"],
    vars_map: dict[str, str],
    env: dict[str, str] | None = None,
    *,
    pg1_path: str | None = None,
) -> str:
    """INI content for the managed pgBackRest drop-in (stanza + repo1 S3).

    Optional process env (same dict as deploy ``env_add``) can override tuning via
    ``PGBR_REPO1_BUNDLE``, ``PGBR_REPO1_BLOCK``, ``PGBR_PROCESS_MAX``, ``PGBR_ARCHIVE_ASYNC``,
    ``PGBR_COMPRESS_LEVEL``, ``PGBR_REPO1_RETENTION_FULL_TYPE``, ``PGBR_REPO1_RETENTION_FULL``.
    ``PGBR_S3_*_RETENTION_FULL`` (suffix ``RETENTION_FULL``) still overrides the numeric
    retention when set.
    """
    env = env or {}
    lines: list[str] = [
        "# Managed by deploy tooling (PGBR_S3_*).",
        "[global]",
        "repo1-type=s3",
    ]
    for suffix, opt in SUFFIX_TO_GLOBAL.items():
        val = vars_map.get(suffix)
        if not val:
            continue
        if suffix == "ENDPOINT" and not val:
            continue
        lines.append(f"{opt}={val}")

    lines.append(f"repo1-bundle={_env_yn(env, 'PGBR_REPO1_BUNDLE', default=True)}")
    lines.append(f"repo1-block={_env_yn(env, 'PGBR_REPO1_BLOCK', default=True)}")
    lines.append(f"process-max={_env_str(env, 'PGBR_PROCESS_MAX', '2')}")
    lines.append(f"archive-async={_env_yn(env, 'PGBR_ARCHIVE_ASYNC', default=True)}")
    lines.append(
        f"repo1-retention-full-type={_env_str(env, 'PGBR_REPO1_RETENTION_FULL_TYPE', 'time')}"
    )
    ret_full = vars_map.get("RETENTION_FULL") or _env_str(env, "PGBR_REPO1_RETENTION_FULL", "30")
    lines.append(f"repo1-retention-full={ret_full}")

    from catalpa_tooling.docker_host_tls import BACKUP_CA_CONTAINER_PATH, backup_ca_host_path

    if backup_ca_host_path(env):
        lines.append(f"repo1-storage-ca-file={BACKUP_CA_CONTAINER_PATH}")

    lines.append("")
    lines.append("[global:archive-push]")
    lines.append(f"compress-level={_env_str(env, 'PGBR_COMPRESS_LEVEL', '3')}")

    stanza = vars_map["STANZA"]
    data_path = (pg1_path or DEFAULT_PGBR_PG1_PATH).strip()
    lines.extend(
        [
            "",
            f"[{stanza}]",
            f"pg1-path={data_path}",
        ]
    )
    return "\n".join(lines) + "\n"


def render_postgres_archive_conf(stanza: str) -> str:
    """Drop-in for WAL archiving (WRITE mode only)."""
    # Stanza is validated to be non-empty; shell-safe for archive_command.
    return (
        f"# Managed by deploy tooling (PGBR_S3_WRITE_*).\n"
        f"wal_level = replica\n"
        f"archive_mode = on\n"
        f"archive_command = 'pgbackrest --stanza={stanza} archive-push %p'\n"
    )


def minimal_pgbackrest_baseline() -> str:
    """Reset state when no PGBR_S3_* vars are set."""
    return "# Managed by deploy tooling (no PGBR_S3_* — baseline).\n[global]\n"


def conflict_error_message(env: dict[str, str]) -> str | None:
    """If both WRITE and READ prefixes are set, return an error string."""
    if resolve_mode(env) != "both":
        return None
    w = _nonempty_prefix_keys(env, PREFIX_WRITE)
    r = _nonempty_prefix_keys(env, PREFIX_READ)
    return (
        "pgBackRest: PGBR_S3_WRITE_* and PGBR_S3_READ_* are mutually exclusive.\n"
        f"  Found WRITE: {w}\n"
        f"  Found READ: {r}"
    )


def _volume_bind_host_path(inspect_entry: dict) -> str | None:
    """Host path when the volume uses local driver bind opts; else ``None``."""
    opts = inspect_entry.get("Options")
    if not isinstance(opts, dict):
        return None
    if str(opts.get("o") or "").strip().lower() != "bind":
        return None
    device = str(opts.get("device") or "").strip()
    if not device:
        return None
    return device.rstrip("/")


def _ssh_target_from_docker_env(docker_env: dict[str, str]) -> str | None:
    host = str(docker_env.get("DOCKER_HOST") or "").strip()
    if not host:
        return None
    try:
        return parse_docker_host_to_ssh_target(host)
    except ValueError:
        return None


def _verify_host_path_on_deploy_host(
    path: str,
    docker_env: dict[str, str],
    *,
    create: bool = False,
    label: str = "storage",
) -> None:
    """Ensure ``path`` exists (and is writable) on the machine running the Docker daemon."""
    norm = path.rstrip("/") or "/"
    q = shlex.quote(norm)
    if create:
        script = f"mkdir -p {q}"
        ssh_target = _ssh_target_from_docker_env(docker_env)
        if ssh_target:
            cmd = ["ssh", ssh_target, script]
        else:
            cmd = ["/bin/sh", "-c", script]
        print(f"$ {format_shell_command(cmd)}", file=sys.stderr)
        run_cmd(cmd, check=True, print_cmd=False)
    check_script = f"test -d {q} && test -w {q}"
    ssh_target = _ssh_target_from_docker_env(docker_env)
    if ssh_target:
        cmd = ["ssh", ssh_target, check_script]
    else:
        cmd = ["/bin/sh", "-c", check_script]
    print(f"$ {format_shell_command(cmd)}", file=sys.stderr)
    r = run_cmd(cmd, check=False, print_cmd=False)
    if r.returncode != 0:
        if create:
            hint = f"Host path {norm!r} is not a writable directory after mkdir."
        else:
            hint = (
                f"Host path {norm!r} is missing or not writable on the deploy host. "
                "Mount storage externally or run `dk <env> storage ensure` with a "
                "`digitalocean` block to provision and mount a block volume."
            )
        print(f"{label}: {hint}", file=sys.stderr)
        raise subprocess.CalledProcessError(r.returncode, cmd)


def _ensure_volume(
    name: str,
    docker_env: dict[str, str],
    *,
    host_path: str | None = None,
    create_host_path: bool = False,
    label: str = "ensure_volumes",
    compose_project: str | None = None,
    compose_volume_key: str | None = None,
) -> None:
    norm_host = (host_path or "").strip().rstrip("/") if host_path else None
    compose_labels = _compose_volume_create_label_args(
        compose_project or "",
        compose_volume_key or "",
    )
    if norm_host:
        _verify_host_path_on_deploy_host(
            norm_host,
            docker_env,
            create=create_host_path,
            label=label,
        )

    r = run_cmd(
        ["docker", "volume", "inspect", name],
        env=docker_env,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode == 0:
        if norm_host:
            try:
                payload = json.loads(r.stdout or "[]")
            except json.JSONDecodeError:
                payload = []
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                existing = _volume_bind_host_path(payload[0])
                if existing is None:
                    print(
                        f"{label}: Docker volume {name!r} exists without a host bind mount. "
                        "Stop the stack, back up data, `docker volume rm` the volume, "
                        "ensure the host mount path exists, then re-run `ensure_volumes`.",
                        file=sys.stderr,
                    )
                    raise subprocess.CalledProcessError(1, ["docker", "volume", "inspect", name])
                if existing != norm_host:
                    print(
                        f"{label}: Docker volume {name!r} is bound to {existing!r}, "
                        f"but info.yaml requests {norm_host!r}. "
                        "Migrate data manually or align info.yaml with the existing bind target.",
                        file=sys.stderr,
                    )
                    raise subprocess.CalledProcessError(1, ["docker", "volume", "inspect", name])
        return

    if norm_host:
        run_cmd(
            [
                "docker",
                "volume",
                "create",
                "--driver",
                "local",
                "--opt",
                "type=none",
                "--opt",
                f"device={norm_host}",
                "--opt",
                "o=bind",
                *compose_labels,
                name,
            ],
            env=docker_env,
            check=True,
        )
    else:
        run_cmd(
            ["docker", "volume", "create", *compose_labels, name],
            env=docker_env,
            check=True,
        )


def external_stack_volume_names(
    env: dict[str, str], *, config: ProjectConfig | None = None
) -> tuple[str, ...]:
    """Named volumes declared ``external: true`` in compose.yml (must exist before ``compose up``)."""
    pg, pgb = volume_names(env, config=config)
    return (
        postgres_data_volume_name(env, config=config),
        django_media_volume_name(env, config=config),
        caddy_data_volume_name(env, config=config),
        pg,
        pgb,
    )


def ensure_external_stack_volumes(
    env: dict[str, str],
    *,
    dry_run: bool = False,
    config: ProjectConfig | None = None,
    volume_hosts: dict[str, str] | None = None,
    create_host_paths: dict[str, bool] | None = None,
) -> int:
    """Create stack external volumes if missing (``docker volume create``). Idempotent.

    Uses the same ``DOCKER_HOST`` (if any) as ``docker compose`` for this deploy.
    ``volume_hosts`` maps compose volume keys (e.g. ``django_media``) to host mount paths
    for local-driver bind named volumes.
    """
    volume_hosts = volume_hosts or {}
    create_host_paths = create_host_paths or {}
    volumes = _stack_volumes_with_compose_keys(env, config=config)
    names = tuple(name for name, _ in volumes)
    name_to_host: dict[str, str] = {}
    name_to_create: dict[str, bool] = {}
    name_to_key = dict(volumes)
    for key, host_path in volume_hosts.items():
        vol_name = stack_volume_docker_name(env, key, config=config)
        name_to_host[vol_name] = host_path
        name_to_create[vol_name] = bool(create_host_paths.get(key))

    if dry_run:
        parts = list(names)
        if name_to_host:
            bind_desc = ", ".join(
                f"{n}→{name_to_host[n]!r}" for n in names if n in name_to_host
            )
            print(
                "ensure_volumes (dry-run): would create missing volumes: "
                + ", ".join(parts)
                + (f"; host binds: {bind_desc}" if bind_desc else ""),
                file=sys.stderr,
            )
        else:
            print(
                "ensure_volumes (dry-run): would create missing volumes: " + ", ".join(parts),
                file=sys.stderr,
            )
        return 0
    docker_env = _docker_env_for_remote(env)
    compose_project = _compose_project_name(env, config)
    for n in names:
        try:
            _ensure_volume(
                n,
                docker_env,
                host_path=name_to_host.get(n),
                create_host_path=name_to_create.get(n, False),
                label="ensure_volumes",
                compose_project=compose_project,
                compose_volume_key=name_to_key[n],
            )
        except subprocess.CalledProcessError as e:
            print(f"ensure_volumes: docker volume failed for {n!r}: {e}", file=sys.stderr)
            return 1
    print("ensure_volumes: ok " + ", ".join(names), file=sys.stderr)
    return 0


def db_compose_volume_names(
    env: dict[str, str], *, config: ProjectConfig | None = None
) -> tuple[str, ...]:
    """External volumes mounted by the compose ``db`` service (PGDATA + pgBackRest conf)."""
    pg_conf, pgb_conf = volume_names(env, config=config)
    return (
        postgres_data_volume_name(env, config=config),
        pg_conf,
        pgb_conf,
    )


def ensure_db_compose_volumes(
    env: dict[str, str],
    *,
    config: ProjectConfig | None = None,
    volume_hosts: dict[str, str] | None = None,
    create_host_paths: dict[str, bool] | None = None,
) -> int:
    """Create external volumes the ``db`` service mounts if missing (``docker volume create``).

    Idempotent. Used before ``docker compose up -d db`` when external volumes are declared in
    compose.yml (same ``DOCKER_HOST`` as other deploy volume ops).
    """
    volume_hosts = volume_hosts or {}
    create_host_paths = create_host_paths or {}
    docker_env = _docker_env_for_remote(env)
    pg_data_key = config.ops.pgbackrest.data_volume if config else "postgres_data"
    compose_project = _compose_project_name(env, config)
    for name, volume_key in _db_volumes_with_compose_keys(env, config=config):
        host_path = None
        create_host = False
        if volume_key == pg_data_key:
            host_path = volume_hosts.get(pg_data_key)
            create_host = bool(create_host_paths.get(pg_data_key))
        try:
            _ensure_volume(
                name,
                docker_env,
                host_path=host_path,
                create_host_path=create_host,
                label="ensure_db_volumes",
                compose_project=compose_project,
                compose_volume_key=volume_key,
            )
        except subprocess.CalledProcessError as e:
            print(
                f"ensure_db_volumes: docker volume failed for {name!r}: {e}",
                file=sys.stderr,
            )
            return 1
    return 0


def ensure_postgres_data_volume(
    env: dict[str, str],
    *,
    config: ProjectConfig | None = None,
    volume_hosts: dict[str, str] | None = None,
    create_host_paths: dict[str, bool] | None = None,
) -> int:
    """Create the stack PGDATA named volume if missing (``docker volume create``). Idempotent.

    Used by ``bkp_db restore`` so ``docker compose run db`` has a mount target for PGDATA.
    Uses the same ``DOCKER_HOST`` as ``ensure_volumes`` and other deploy volume ops.
    """
    volume_hosts = volume_hosts or {}
    create_host_paths = create_host_paths or {}
    docker_env = _docker_env_for_remote(env)
    name = postgres_data_volume_name(env, config=config)
    pg_data_key = config.ops.pgbackrest.data_volume if config else "postgres_data"
    try:
        _ensure_volume(
            name,
            docker_env,
            host_path=volume_hosts.get(pg_data_key),
            create_host_path=bool(create_host_paths.get(pg_data_key)),
            label="pgBackRest restore",
            compose_project=_compose_project_name(env, config),
            compose_volume_key=pg_data_key,
        )
    except subprocess.CalledProcessError as e:
        print(
            f"pgBackRest restore: docker volume failed for {name!r}: {e}",
            file=sys.stderr,
        )
        return 1
    return 0


def remove_wipe_data_volumes(env: dict[str, str], *, config: ProjectConfig | None = None) -> int:
    """Remove external PGDATA and ``django_media`` volumes after ``compose down -v``.

    Compose does not delete ``external:`` volumes; this finishes a destructive wipe of database
    PGDATA and Django/Wagtail uploads. Uses the same ``DOCKER_HOST`` as other deploy volume ops.

    Missing volumes (e.g. already removed) are treated as success.
    """
    docker_env = _docker_env_for_remote(env)
    targets = (
        postgres_data_volume_name(env, config=config),
        django_media_volume_name(env, config=config),
    )
    for name in targets:
        r = run_cmd(
            ["docker", "volume", "rm", name],
            env=docker_env,
            capture_output=True,
            text=True,
            check=False,
            print_cmd=True,
        )
        if r.returncode == 0:
            print(f"wipe: removed volume {name}", file=sys.stderr)
            continue
        combined = ((r.stderr or "") + (r.stdout or "")).lower()
        if "no such volume" in combined:
            print(f"wipe: volume {name} already absent (skipped)", file=sys.stderr)
            continue
        print(
            f"wipe: docker volume rm failed for {name!r}: {r.stderr or r.stdout}",
            file=sys.stderr,
        )
        return 1
    return 0


def _docker_run_volume_work_args() -> list[str]:
    """One-off volume writes use the db image (often USER postgres) against root-owned named volumes."""
    return [
        *_compose_db_platform_args(),
        "--user",
        "0",
    ]


def _docker_run_cp(
    volume: str,
    dest_name: str,
    content: str,
    docker_env: dict[str, str],
    *,
    image: str,
) -> None:
    # Stream config into the container stdin — no bind mounts from the machine running the CLI.
    # With DOCKER_HOST=ssh://…, host paths (e.g. macOS /var/folders/…) are resolved on the *remote*
    # daemon host, so mounting a local temp file/dir fails. Docker Desktop also mishandles some
    # single-file mounts. `docker run -i` + stdin works for local and remote engines.
    q = shlex.quote(dest_name)
    run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            *_docker_run_volume_work_args(),
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{volume}:/work",
            image,
            "-c",
            (
                f"cat > /work/{q} && chmod 644 /work/{q} "
                f"&& chown postgres:postgres /work/{q}"
            ),
        ],
        env=docker_env,
        input=content.encode("utf-8"),
        check=True,
    )


def _docker_run_rm(
    volume: str,
    filename: str,
    docker_env: dict[str, str],
    *,
    image: str,
) -> None:
    q = shlex.quote(filename)
    run_cmd(
        [
            "docker",
            "run",
            "--rm",
            *_docker_run_volume_work_args(),
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{volume}:/work",
            image,
            "-c",
            f"rm -f /work/{q}",
        ],
        env=docker_env,
        check=False,
    )


def _docker_run_rm_other_confs(
    volume: str,
    keep_filename: str,
    docker_env: dict[str, str],
    *,
    image: str,
) -> None:
    """Remove other ``*.conf`` drop-ins so pgBackRest/Postgres do not merge duplicate keys."""
    keep = shlex.quote(keep_filename)
    run_cmd(
        [
            "docker",
            "run",
            "--rm",
            *_docker_run_volume_work_args(),
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{volume}:/work",
            image,
            "-c",
            (
                f"for f in /work/*.conf; do "
                f'[ -e "$f" ] || continue; '
                f'[ "$(basename "$f")" = {keep} ] && continue; '
                f'rm -f "$f"; '
                f"done"
            ),
        ],
        env=docker_env,
        check=False,
    )


def _docker_env_for_remote(env: dict[str, str]) -> dict[str, str]:
    out = os.environ.copy()
    if env.get("DOCKER_HOST"):
        out["DOCKER_HOST"] = env["DOCKER_HOST"]
    return out


def stanza_from_env_for_pgbackrest(env: dict[str, str]) -> str | None:
    """Stanza name from PGBR_S3_WRITE_* or PGBR_S3_READ_* when mode is write/read."""
    mode = resolve_mode(env)
    if mode == "write":
        return _extract_stanza_vars(env, PREFIX_WRITE).get("STANZA")
    if mode == "read":
        return _extract_stanza_vars(env, PREFIX_READ).get("STANZA")
    return None


def stanza_create_allowed(env: dict[str, str]) -> bool:
    """True only in WRITE mode — ``stanza-create`` mutates the repository and must not run on READ hosts."""
    return resolve_mode(env) == "write"


def run_pgbackrest_verify(env: dict[str, str], *, image: str) -> int:
    """Preflight for ``bkp_db configure verify``: ``pgbackrest version`` against the conf volume.

    Online ``pgbackrest check`` requires a running PostgreSQL and must run in the ``db`` container
    (see ``run_configure_verify_online_check`` in ``pgbackrest_db``); a throwaway ``docker run``
    only has the config volume, not a server socket.
    """
    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    docker_env = _docker_env_for_remote(env)
    vol_pgb = volume_names(env)[1]
    mode = resolve_mode(env)

    ver = run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "pgbackrest",
            "-v",
            f"{vol_pgb}:/etc/pgbackrest/conf.d",
            image,
            "version",
        ],
        env=docker_env,
        check=False,
    )
    if ver.returncode != 0:
        log("pgBackRest: pgbackrest version failed.")
        return ver.returncode

    if mode == "none":
        log("pgBackRest verify: mode is none (no PGBR_S3_*); skipped stanza check.")
        return 0

    stanza = stanza_from_env_for_pgbackrest(env)
    if not stanza:
        log("pgBackRest verify: could not determine STANZA from env.")
        return 1

    return 0


def parse_pgbackrest_info_stanza_healthy(
    stdout: str, stderr: str, *, stanza: str
) -> bool:
    """True when ``pgbackrest info`` output shows the stanza ``status: ok`` (repo metadata valid).

    ``pgbackrest info`` often exits 0 even when the stanza row is ``status: error (…)``; do not
    rely on the process exit code alone.
    """
    text = (stdout or "").strip()
    if text.startswith("[") or text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if payload is not None:
            stanzas = payload if isinstance(payload, list) else [payload]
            for entry in stanzas:
                if not isinstance(entry, dict):
                    continue
                if entry.get("name") != stanza:
                    continue
                status = entry.get("status")
                if isinstance(status, dict):
                    return status.get("code") == 0
                if isinstance(status, str):
                    return status.strip().lower() == "ok"
            return False
    body = f"{stdout or ''}\n{stderr or ''}"
    match = re.search(
        rf"stanza:\s*{re.escape(stanza)}\s*\n\s*status:\s*(\S+)",
        body,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if not match:
        return False
    return match.group(1).lower() == "ok"


def pgbackrest_stanza_exists_in_repo(
    env: dict[str, str], *, image: str, config: ProjectConfig | None = None
) -> bool:
    """True when ``pgbackrest info`` reports the stanza healthy (``status: ok`` in the repository)."""
    if resolve_mode(env) != "write":
        return False
    stanza = stanza_from_env_for_pgbackrest(env)
    if not stanza:
        return False
    docker_env = _docker_env_for_remote(env)
    vol_pgb = volume_names(env, config=config)[1]
    sq = shlex.quote(stanza)
    r = run_cmd(
        [
            "docker",
            "run",
            "--rm",
            *_compose_db_platform_args(),
            *_docker_run_s3_network_args(env),
            "--entrypoint",
            "pgbackrest",
            "-v",
            f"{vol_pgb}:/etc/pgbackrest/conf.d",
            image,
            f"--stanza={sq}",
            "--output=json",
            "info",
        ],
        env=docker_env,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode != 0 and not (r.stdout or r.stderr):
        return False
    healthy = parse_pgbackrest_info_stanza_healthy(
        r.stdout or "", r.stderr or "", stanza=stanza
    )
    if healthy:
        return True
    # Older pgbackrest without --output=json: retry plain text info.
    if (r.stdout or "").strip().startswith(("[", "{")):
        return False
    r_text = run_cmd(
        [
            "docker",
            "run",
            "--rm",
            *_compose_db_platform_args(),
            "--entrypoint",
            "pgbackrest",
            "-v",
            f"{vol_pgb}:/etc/pgbackrest/conf.d",
            image,
            f"--stanza={sq}",
            "info",
        ],
        env=docker_env,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    return parse_pgbackrest_info_stanza_healthy(
        r_text.stdout or "", r_text.stderr or "", stanza=stanza
    )


def run_pgbackrest_stanza_create(
    env: dict[str, str], *, image: str, config: ProjectConfig | None = None
) -> int:
    """Run ``pgbackrest stanza-create`` against the repository (WRITE mode only)."""
    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    mode = resolve_mode(env)
    if mode == "none":
        log("pgBackRest stanza-create: set PGBR_S3_WRITE_* (full repo vars) first.")
        return 1
    if mode == "read":
        log(
            "pgBackRest stanza-create: not allowed in READ mode (PGBR_S3_READ_*). "
            "It writes stanza metadata to the repository; use only with PGBR_S3_WRITE_* on backup hosts."
        )
        return 1
    if mode == "both":
        log("pgBackRest stanza-create: resolve PGBR_S3_WRITE vs READ conflict first.")
        return 1

    stanza = stanza_from_env_for_pgbackrest(env)
    if not stanza:
        log("pgBackRest stanza-create: STANZA missing from env.")
        return 1

    docker_env = _docker_env_for_remote(env)
    vol_pgb = volume_names(env)[1]
    vol_data = postgres_data_volume_name(env, config=config)
    pg1 = postgres_pg1_path(env, config=config)
    mount = pgdata_volume_mount(pg1)
    if not _pgdata_has_control_file(docker_env, image, vol_data, pg1_path=pg1):
        log(
            "pgBackRest stanza-create: PostgreSQL PGDATA is missing or not initialized "
            f"(no global/pg_control in Docker volume {vol_data!r}). "
            "Run `dk <env> bkp_db init` or `dk <env> bkp_db configure stanza-create` "
            "(starts `db` automatically), or `dk <env> up -d db` then retry."
        )
        return 1

    sq = shlex.quote(stanza)
    r = run_cmd(
        [
            "docker",
            "run",
            "--rm",
            *_compose_db_platform_args(),
            *_docker_run_s3_network_args(env),
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{vol_pgb}:/etc/pgbackrest/conf.d",
            "-v",
            f"{vol_data}:{mount}",
            image,
            "-c",
            f"pgbackrest --stanza={sq} --no-online stanza-create",
        ],
        env=docker_env,
        check=False,
    )
    if r.returncode != 0:
        log("pgBackRest: stanza-create failed.")
    return r.returncode


def pgbackrest_managed_conf_materialized(
    env: dict[str, str], *, config: ProjectConfig | None = None
) -> bool:
    """True when the pgbackrest_conf volume has the managed drop-in with ``pg1-path``."""
    mode = resolve_mode(env)
    if mode == "none":
        return False

    conf_name = _require_pgbackrest_conf_name(config)
    vol_pgb = volume_names(env, config=config)[1]
    image = postgres_image_from_env(env, config=config)
    docker_env = _docker_env_for_remote(env)
    conf_path = f"/etc/pgbackrest/conf.d/{conf_name}"
    inner = (
        f"test -f {shlex.quote(conf_path)}"
        f' && grep -q "^pg1-path=" {shlex.quote(conf_path)}'
    )
    r = run_cmd(
        [
            "docker",
            "run",
            "--rm",
            *_compose_db_platform_args(),
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{vol_pgb}:/etc/pgbackrest/conf.d",
            image,
            "-c",
            inner,
        ],
        env=docker_env,
        capture_output=True,
        check=False,
        print_cmd=False,
    )
    return r.returncode == 0


def ensure_pgbackrest_conf_before_restore(
    env: dict[str, str],
    *,
    config: ProjectConfig | None = None,
    skip_configure_confirm: bool = False,
) -> int:
    """Ensure managed pgBackRest config on the deploy host matches credentials before offline restore.

    When the ``pgbackrest_conf`` volume is empty, offers to run ``materialize_configs``.
    When materialized config differs from current ``PGBR_S3_READ_*`` / ``PGBR_S3_WRITE_*`` env,
    prints a diff and re-materializes (same prompt rules). With ``skip_configure_confirm``
    (global ``dk --yes``), configures without a y/n prompt.
    """
    conflict = conflict_error_message(env)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1

    mode = resolve_mode(env)
    if mode == "none":
        vol_pgb = volume_names(env, config=config)[1]
        print(
            "pgBackRest restore: pgBackRest config is not on the deploy host "
            f"({vol_pgb!r} missing managed stanza config). "
            "Set PGBR_S3_READ_* or PGBR_S3_WRITE_* in credentials, then run "
            "`dk <env> db configure`.",
            file=sys.stderr,
        )
        return 1

    expected = expected_pgbackrest_repo_settings(env, config=config)
    if expected is None:
        prefix = PREFIX_WRITE if mode == "write" else PREFIX_READ
        vars_map = _extract_stanza_vars(env, prefix)
        missing_err = _validate_repo_vars(vars_map, mode=mode)
        if missing_err:
            print(missing_err, file=sys.stderr)
        return 1

    volume = read_managed_pgbackrest_repo_settings(env, config=config)
    if volume and repo_settings_match(volume, expected):
        return 0

    vol_pgb = volume_names(env, config=config)[1]
    conf_name = _require_pgbackrest_conf_name(config)
    if volume:
        print(
            "pgBackRest restore: volume config does not match current credentials "
            f"({vol_pgb!r} / {conf_name}).",
            file=sys.stderr,
        )
        print(
            f"  volume:  stanza={volume.stanza!r} repo1-path={volume.repo_path!r} "
            f"bucket={volume.bucket!r}",
            file=sys.stderr,
        )
        print(
            f"  env:     stanza={expected.stanza!r} repo1-path={expected.repo_path!r} "
            f"bucket={expected.bucket!r}",
            file=sys.stderr,
        )
    else:
        print(
            "pgBackRest restore: managed config is missing on the deploy host "
            f"({vol_pgb!r} has no {conf_name} with pg1-path).",
            file=sys.stderr,
        )

    if not skip_configure_confirm:
        from catalpa_tooling.cli_confirm import confirm_yes_default_no

        if not confirm_yes_default_no(
            "Run `db configure` now to write pgBackRest config into that volume? (y/N): "
        ):
            print(
                "Cancelled. Run `dk <env> db configure`, then retry restore.",
                file=sys.stderr,
            )
            return 1
    else:
        print(
            "pgBackRest restore: running `db configure` (--yes)…",
            file=sys.stderr,
        )

    return materialize_configs(
        env,
        dry_run=False,
        postgres_image=postgres_image_from_env(env, config=config),
        config=config,
    )


def materialize_configs(
    env: dict[str, str],
    *,
    dry_run: bool = False,
    postgres_image: str | None = None,
    config: ProjectConfig | None = None,
) -> int:
    """Create or update named volumes used by compose for Postgres / pgBackRest configs.

    Uses the same ``DOCKER_HOST`` (if any) as ``docker compose``. Idempotent.
    One-off steps use the stack **db** image (``postgres_image``) so permissions
    and tooling match the running database container.
    """
    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    image = postgres_image if postgres_image is not None else postgres_image_from_env(env, config=config)
    ops = _require_pgbackrest_ops(config)
    postgres_conf = ops.postgres_conf
    pgbackrest_conf = ops.pgbackrest_conf

    conflict = conflict_error_message(env)
    if conflict:
        log(conflict)
        return 1
    mode = resolve_mode(env)
    vol_pg, vol_pgb = volume_names(env, config=config)
    docker_env = _docker_env_for_remote(env)

    if dry_run:
        log(
            f"pgBackRest volume config (dry-run): mode={mode} image={image} "
            f"volumes {vol_pg}, {vol_pgb}"
        )
        return 0

    try:
        compose_project = _compose_project_name(env, config)
        _ensure_volume(
            vol_pg,
            docker_env,
            compose_project=compose_project,
            compose_volume_key="postgres_conf",
        )
        _ensure_volume(
            vol_pgb,
            docker_env,
            compose_project=compose_project,
            compose_volume_key="pgbackrest_conf",
        )
    except subprocess.CalledProcessError as e:
        log(f"pgBackRest: docker volume failed: {e}")
        return 1

    try:
        if mode == "none":
            _docker_run_rm_other_confs(vol_pg, postgres_conf, docker_env, image=image)
            _docker_run_rm(vol_pg, postgres_conf, docker_env, image=image)
            _docker_run_rm_other_confs(vol_pgb, pgbackrest_conf, docker_env, image=image)
            _docker_run_cp(
                vol_pgb,
                pgbackrest_conf,
                minimal_pgbackrest_baseline(),
                docker_env,
                image=image,
            )
            return 0

        prefix = PREFIX_WRITE if mode == "write" else PREFIX_READ
        vars_map = _extract_stanza_vars(env, prefix)
        missing_err = _validate_repo_vars(vars_map, mode=mode)
        if missing_err:
            log(missing_err)
            return 1

        pg1 = postgres_pg1_path(env, config=config)
        pgbr_content = render_pgbackrest_ini(mode, vars_map, env, pg1_path=pg1)
        _docker_run_rm_other_confs(vol_pgb, pgbackrest_conf, docker_env, image=image)
        _docker_run_cp(vol_pgb, pgbackrest_conf, pgbr_content, docker_env, image=image)

        if mode == "write":
            _docker_run_rm_other_confs(vol_pg, postgres_conf, docker_env, image=image)
            _docker_run_cp(
                vol_pg,
                postgres_conf,
                render_postgres_archive_conf(vars_map["STANZA"]),
                docker_env,
                image=image,
            )
        else:
            _docker_run_rm(vol_pg, postgres_conf, docker_env, image=image)

    except subprocess.CalledProcessError as e:
        log(f"pgBackRest: docker run failed: {e}")
        return 1

    return 0


def should_materialize_for_compose(compose_args: list[str]) -> bool:
    """True when deploy runs ``compose up`` (stack start / recreate)."""
    if not compose_args:
        return True
    return compose_args[0] == "up"
