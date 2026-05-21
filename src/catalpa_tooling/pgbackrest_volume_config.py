"""Deploy-time Postgres / pgBackRest named-volume config from PGBR_S3_* env (see README_PGBACKREST.md)."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from typing import Literal

from catalpa_tooling.config import DEFAULT_PGBR_PG1_PATH, ProjectConfig
from catalpa_tooling.run_cmd import run as run_cmd

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
}
REQUIRED_SUFFIXES = frozenset({"BUCKET", "REGION", "KEY", "SECRET", "REPO_PATH", "STANZA"})

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
    if config is not None:
        return config.stack.compose_project_default
    return "pas_indmo"


def postgres_image_from_env(env: dict[str, str], *, config: ProjectConfig | None = None) -> str:
    """Resolved db stack image ref (same defaults as ``compose.yml`` ``db`` service)."""
    explicit = (env.get("Postgres_IMAGE") or "").strip()
    if explicit:
        return explicit
    default_reg = config.ops.pgbackrest.default_registry if config else "ghcr.io/catalpainternational/pas_indmo"
    reg = (env.get("STACK_IMAGE_REGISTRY") or default_reg).strip().rstrip("/") or default_reg
    tag = (env.get("STACK_IMAGE_TAG") or "latest").strip() or "latest"
    db_image = config.image_component("db") if config else "indmo-postgres"
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
    """Docker volume name for ``django_media`` (same as compose.yml ``name:``)."""
    project = _compose_project_name(env, config)
    return f"{project}_django_media"


def caddy_data_volume_name(env: dict[str, str], *, config: ProjectConfig | None = None) -> str:
    """Docker volume name for ``caddy_data`` (same as compose.yml ``name:``)."""
    project = _compose_project_name(env, config)
    return f"{project}_caddy_data"


def _compose_db_platform_args() -> list[str]:
    """Match ``compose.yml`` ``db`` service ``platform: linux/amd64`` for one-off ``docker run``."""
    return ["--platform", "linux/amd64"]


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
    """INI content for ``50-indmo-managed.conf`` (stanza + repo1 S3).

    Optional process env (same dict as deploy ``env_add``) can override tuning via
    ``PGBR_REPO1_BUNDLE``, ``PGBR_REPO1_BLOCK``, ``PGBR_PROCESS_MAX``, ``PGBR_ARCHIVE_ASYNC``,
    ``PGBR_COMPRESS_LEVEL``, ``PGBR_REPO1_RETENTION_FULL_TYPE``, ``PGBR_REPO1_RETENTION_FULL``.
    ``PGBR_S3_*_RETENTION_FULL`` (suffix ``RETENTION_FULL``) still overrides the numeric
    retention when set.
    """
    env = env or {}
    lines: list[str] = [
        "# Managed by indmo deploy (PGBR_S3_*).",
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
        f"# Managed by indmo deploy (PGBR_S3_WRITE_*).\n"
        f"wal_level = replica\n"
        f"archive_mode = on\n"
        f"archive_command = 'pgbackrest --stanza={stanza} archive-push %p'\n"
    )


def minimal_pgbackrest_baseline() -> str:
    """Reset state when no PGBR_S3_* vars are set."""
    return "# Managed by indmo deploy (no PGBR_S3_* — baseline).\n[global]\n"


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


def _ensure_volume(name: str, docker_env: dict[str, str]) -> None:
    r = run_cmd(
        ["docker", "volume", "inspect", name],
        env=docker_env,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode == 0:
        return
    run_cmd(["docker", "volume", "create", name], env=docker_env, check=True)


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
    env: dict[str, str], *, dry_run: bool = False, config: ProjectConfig | None = None
) -> int:
    """Create stack external volumes if missing (``docker volume create``). Idempotent.

    Uses the same ``DOCKER_HOST`` (if any) as ``docker compose`` for this deploy.
    """
    names = external_stack_volume_names(env, config=config)
    if dry_run:
        print(
            "ensure_volumes (dry-run): would create missing volumes: " + ", ".join(names),
            file=sys.stderr,
        )
        return 0
    docker_env = _docker_env_for_remote(env)
    for n in names:
        try:
            _ensure_volume(n, docker_env)
        except subprocess.CalledProcessError as e:
            print(f"ensure_volumes: docker volume failed for {n!r}: {e}", file=sys.stderr)
            return 1
    print("ensure_volumes: ok " + ", ".join(names), file=sys.stderr)
    return 0


def ensure_postgres_data_volume(env: dict[str, str], *, config: ProjectConfig | None = None) -> int:
    """Create the stack PGDATA named volume if missing (``docker volume create``). Idempotent.

    Used by ``bkp_db restore`` so ``docker compose run db`` has a mount target for PGDATA.
    Uses the same ``DOCKER_HOST`` as ``ensure_volumes`` and other deploy volume ops.
    """
    docker_env = _docker_env_for_remote(env)
    name = postgres_data_volume_name(env, config=config)
    try:
        _ensure_volume(name, docker_env)
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
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{volume}:/work",
            image,
            "-c",
            f"cat > /work/{q} && chmod 644 /work/{q}",
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
    run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{volume}:/work",
            image,
            "-c",
            f"rm -f /work/{filename}",
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
            "Even with --no-online, pgBackRest needs the data directory on disk. "
            "Start the db service once (initdb), e.g. from the repo root with the same "
            "COMPOSE_PROJECT_NAME as this deploy:\n"
            "  docker compose -f compose.yml up -d db\n"
            "Then retry."
        )
        return 1

    sq = shlex.quote(stanza)
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

    conf_name = (
        config.ops.pgbackrest.pgbackrest_conf if config else "50-indmo-managed.conf"
    )
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
    """Ensure managed pgBackRest config exists on the deploy host before offline restore.

    When the ``pgbackrest_conf`` volume is empty or only has a baseline ``[global]`` stub,
    offers to run ``materialize_configs`` (same as ``bkp_db configure``). With
    ``skip_configure_confirm`` (global ``dk --yes``), configures without a y/n prompt.
    """
    if pgbackrest_managed_conf_materialized(env, config=config):
        return 0

    vol_pgb = volume_names(env, config=config)[1]
    mode = resolve_mode(env)
    if mode == "none":
        print(
            "pgBackRest restore: pgBackRest config is not on the deploy host "
            f"({vol_pgb!r} missing managed stanza config). "
            "Set PGBR_S3_READ_* or PGBR_S3_WRITE_* in credentials, then run "
            "`dk <env> bkp_db configure`.",
            file=sys.stderr,
        )
        return 1

    print(
        "pgBackRest restore: managed config is missing on the deploy host "
        f"({vol_pgb!r} has no {config.ops.pgbackrest.pgbackrest_conf if config else '50-indmo-managed.conf'} "
        "with pg1-path).",
        file=sys.stderr,
    )
    if not skip_configure_confirm:
        from catalpa_tooling.cli_confirm import confirm_yes_default_no

        if not confirm_yes_default_no(
            "Run `bkp_db configure` now to write pgBackRest config into that volume? (y/N): "
        ):
            print(
                "Cancelled. Run `dk <env> bkp_db configure`, then retry restore.",
                file=sys.stderr,
            )
            return 1
    else:
        print(
            "pgBackRest restore: running `bkp_db configure` (--yes)…",
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
    One-off steps use the **indmo-postgres** image (``postgres_image``) so permissions
    and tooling match the running database container.
    """
    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    image = postgres_image if postgres_image is not None else postgres_image_from_env(env, config=config)
    postgres_conf = config.ops.pgbackrest.postgres_conf if config else "30-indmo-pgbackrest-archive.conf"
    pgbackrest_conf = config.ops.pgbackrest.pgbackrest_conf if config else "50-indmo-managed.conf"

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
        _ensure_volume(vol_pg, docker_env)
        _ensure_volume(vol_pgb, docker_env)
    except subprocess.CalledProcessError as e:
        log(f"pgBackRest: docker volume failed: {e}")
        return 1

    try:
        if mode == "none":
            _docker_run_rm(vol_pg, postgres_conf, docker_env, image=image)
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
        _docker_run_cp(vol_pgb, pgbackrest_conf, pgbr_content, docker_env, image=image)

        if mode == "write":
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
