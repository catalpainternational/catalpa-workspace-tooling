"""Load and validate repo-root ``tooling.yaml`` (project manifest)."""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.backup_logging_levels import (
    BackupLoggingConfigError,
    parse_optional_pgbr_log_level,
    parse_optional_restic_verbose,
)
from catalpa_tooling.deprecation import warn_deprecated
from catalpa_tooling.repo_paths import TOOLING_FILENAME, repo_root_from_cwd

DEFAULT_ROOT_MARKER = "pyproject.toml"
DEFAULT_RESTIC_DATA_VOLUME = "django_media"

DEFAULT_ORIGIN_ENV_KEYS: tuple[str, ...] = (
    "SITE_ORIGIN",
    "DJANGO_ORIGIN",
    "BERO_ORIGIN",
)
DEFAULT_BUILD_PLACEHOLDERS: dict[str, str] = {
    "POSTGRES_PASSWORD": "build_placeholder",
    "DJANGO_DB_PASSWORD": "build_placeholder",
    "METABASE_DB_PASSWORD": "build_placeholder",
    "DJANGO_SECRET_KEY": "build-placeholder-not-for-production",
    "SITE_ORIGIN": "https://build.example",
    "DJANGO_ORIGIN": "https://build.example",
    "BERO_ORIGIN": "https://build.example",
    "METABASE_ORIGIN": "https://build.example",
}
DEFAULT_DEV_SITE_ORIGIN_BASE = "localdev.temp.build"
DEFAULT_DEV_LAN_DNS_SUFFIX = "lan.localdev.temp.build"
DEFAULT_BUILD_TIME_ZONE = "Asia/Dili"


class ProjectConfigError(ValueError):
    """Invalid or missing project manifest."""


def _require_mapping(data: Any, key: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ProjectConfigError(f"Missing or invalid top-level key: {key!r}")
    return data


def _require_str(data: dict[str, Any], key: str, *, section: str = "") -> str:
    raw = data.get(key)
    if raw is None or raw is False:
        prefix = f"{section}." if section else ""
        raise ProjectConfigError(f"Missing required key: {prefix}{key}")
    s = str(raw).strip()
    if not s:
        prefix = f"{section}." if section else ""
        raise ProjectConfigError(f"Empty required key: {prefix}{key}")
    return s


def _optional_bool(data: dict[str, Any], key: str, *, default: bool) -> bool:
    if key not in data or data[key] is None:
        return default
    raw = data[key]
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)) and raw in (0, 1):
        return bool(raw)
    s = str(raw).strip().lower()
    if s in ("true", "yes", "1", "on"):
        return True
    if s in ("false", "no", "0", "off"):
        return False
    raise ProjectConfigError(f"digitalocean.{key} must be a boolean (got {raw!r})")


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    if key not in data or data[key] is None or data[key] is False:
        return None
    s = str(data[key]).strip()
    return s or None


def _validate_rel_path(rel: str, *, field: str) -> str:
    if not rel or ".." in Path(rel).parts:
        raise ProjectConfigError(f"Invalid relative path for {field}: {rel!r}")
    return rel


def _validate_compose_volume_key(name: str, *, field: str) -> str:
    """Compose named-volume key (suffix in ``{project}_{key}``)."""
    s = name.strip()
    if not s or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", s):
        raise ProjectConfigError(
            f"Invalid compose volume key for {field}: {name!r} "
            "(use letters, digits, underscore, dot, hyphen)"
        )
    return s


def _missing_section(section: str, *, needed_for: str) -> ProjectConfigError:
    """Error for a deploy-side section a command needs but the manifest omits.

    Sections such as ``stack:`` and ``ops:`` are optional so that a project can adopt the
    engine-agnostic commands (``dk cut-release``, ``tests compliance``, ``scripts``) without
    inventing compose, pgbackrest, zabbix, and systemd configuration it does not use.
    """
    return ProjectConfigError(
        f"tooling.yaml has no `{section}:` section (or it is empty), which is required for "
        f"{needed_for}. Add a `{section}:` block — catalpa-workspace-tooling's "
        f"tests/fixtures/minimal_project/tooling.yaml is a complete reference manifest."
    )


@dataclass(frozen=True)
class DeployPathsConfig:
    envs_dir: str
    images_config: str
    default_compose: str
    dev_compose: str
    credentials_optional_envs: tuple[str, ...]
    env_aliases: dict[str, str]


@dataclass(frozen=True)
class PathsConfig:
    backend: str
    frontend: str
    prototype: str | None
    scripts: tuple[str, ...]
    env_local: str
    email_backend_dir: str
    media_dir: str | None
    fetch_db_dump: str
    fetch_metabase_db_dump: str | None
    deploy_optional: DeployPathsConfig | None

    @property
    def deploy(self) -> DeployPathsConfig:
        """Deploy paths, or raise when ``paths.deploy`` is absent."""
        if self.deploy_optional is None:
            raise _missing_section(
                "paths.deploy",
                needed_for="deploy and image commands (dk build, dk push, dk <env>, tests ci)",
            )
        return self.deploy_optional


@dataclass(frozen=True)
class StackServicesConfig:
    web: str
    proxy: str
    db: str


@dataclass(frozen=True)
class StackImagesConfig:
    registry_key: str
    components: dict[str, str]


@dataclass(frozen=True)
class StackHealthcheckConfig:
    service: str
    url: str


@dataclass(frozen=True)
class StackConfig:
    compose_project_default: str
    services: StackServicesConfig
    images: StackImagesConfig
    healthcheck: StackHealthcheckConfig
    origin_env_keys: tuple[str, ...]
    build_placeholders: dict[str, str]


# PG 18+ official image layout (see catalpa-postgres-entrypoint.sh); override via tooling.yaml pg1_path.
DEFAULT_PGBR_PG1_PATH = "/var/lib/postgresql/18/docker"
DEFAULT_PGBR_POSTGRES_CONF = "30-pgbackrest-archive.conf"
DEFAULT_PGBR_CONF = "50-managed.conf"
DEFAULT_PGBR_REGISTRY = "ghcr.io/catalpainternational"
DEFAULT_PGBR_DATA_VOLUME = "postgres_data"

DEFAULT_DEPLOY_ENVS_DIR = "docker/envs"
DEFAULT_DEPLOY_IMAGES_CONFIG = "docker/images.yaml"
DEFAULT_DEPLOY_COMPOSE = "compose.yaml"
DEFAULT_DEPLOY_DEV_COMPOSE = "compose.dev.yaml"

DEFAULT_STACK_REGISTRY_KEY = "image_registry"
DEFAULT_STACK_WEB_SERVICE = "django"
DEFAULT_STACK_PROXY_SERVICE = "caddy"
DEFAULT_STACK_DB_SERVICE = "db"

# Bero-family default systemd unit suffixes (no pgbackrest-diff); prefix with systemd_unit_prefix.
DEFAULT_SYSTEMD_PGBACKREST_SUFFIXES: tuple[str, ...] = (
    "pgbackrest-backup-full.service",
    "pgbackrest-backup-incr.service",
    "pgbackrest-backup-full.timer",
    "pgbackrest-backup-incr.timer",
)
DEFAULT_SYSTEMD_RESTIC_SUFFIXES: tuple[str, ...] = (
    "restic-files-backup.service",
    "restic-files-backup.timer",
)


def default_pgbackrest_restore_temp_prefix(project_name: str) -> str:
    """Default ``/tmp`` archive prefix for compose ``pg_restore`` (``ops.pgbackrest.restore_temp_prefix``)."""
    return f"{project_name}_pgrestore_"


def default_fetch_db_dump_path(project_name: str) -> str:
    """Default ``paths.fetch_db_dump`` for Docker-deployed consumers."""
    return f"docker/postgres/dumps/{project_name}_db.custom"


def default_stack_image_components(project_name: str) -> dict[str, str]:
    return {
        "web": f"{project_name}-django",
        "proxy": f"{project_name}-caddy",
        "db": f"{project_name}-db",
    }


def default_systemd_unit_lists(unit_prefix: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(pgbackrest_units, restic_units)`` for a bero-family ``systemd_unit_prefix``."""
    pg = tuple(f"{unit_prefix}{s}" for s in DEFAULT_SYSTEMD_PGBACKREST_SUFFIXES)
    restic = tuple(f"{unit_prefix}{s}" for s in DEFAULT_SYSTEMD_RESTIC_SUFFIXES)
    return pg, restic


def _storage_volume_host_path_from_info(
    repo_root: Path,
    paths: PathsConfig,
    *,
    dk_env: str,
    volume_key: str,
) -> str | None:
    """Return ``storage.volumes.<volume_key>.path`` from ``docker/envs/<dk_env>/info.yaml``."""
    if paths.deploy_optional is None:
        return None
    info_path = repo_root / paths.deploy_optional.envs_dir / dk_env / "info.yaml"
    if not info_path.is_file():
        return None
    try:
        with open(info_path, encoding="utf-8") as f:
            info = yaml.safe_load(f) or {}
    except OSError:
        return None
    if not isinstance(info, dict):
        return None
    storage = info.get("storage")
    if not isinstance(storage, dict):
        return None
    volumes = storage.get("volumes")
    if not isinstance(volumes, dict):
        return None
    spec = volumes.get(volume_key)
    if not isinstance(spec, dict):
        return None
    path = str(spec.get("path") or "").strip()
    return path.rstrip("/") if path.startswith("/") else None


@dataclass(frozen=True)
class PgbackrestOpsConfig:
    postgres_conf: str
    pgbackrest_conf: str
    default_registry: str
    restore_temp_prefix: str
    data_volume: str
    pg1_path: str
    log_level_console: str | None = None
    log_level_stderr: str | None = None
    restore_log_level_console: str | None = None


@dataclass(frozen=True)
class ResticOpsConfig:
    """Compose volume key restic backs up (``{COMPOSE_PROJECT_NAME}_{data_volume}``)."""

    data_volume: str
    backup_path: str | None = None
    verbose: int | None = None
    restore_verbose: int | None = None


@dataclass(frozen=True)
class ZabbixOpsConfig:
    unit_name: str
    userparams_file: str


@dataclass(frozen=True)
class SystemdUnitsOpsConfig:
    pgbackrest: tuple[str, ...]
    restic: tuple[str, ...]
    timers_enable_pgbackrest: tuple[str, ...]
    timers_enable_restic: tuple[str, ...]


@dataclass(frozen=True)
class DbPsqlRestoreEntry:
    """Postgres superuser SQL to run in the ``db`` container after a restore leg."""

    target: str  # ``app`` or ``metabase``
    file: str  # repo-relative path or absolute in-container path


@dataclass(frozen=True)
class PostDbRestoreOpsConfig:
    """Hooks after a successful Compose app-database restore."""

    envs: tuple[str, ...] | None
    db_psql: tuple[DbPsqlRestoreEntry, ...]
    manage_commands: tuple[tuple[str, ...], ...]

    def applies_to_env(self, env_name: str) -> bool:
        if self.envs is None:
            return True
        return env_name in self.envs


@dataclass(frozen=True)
class PostMetabaseDbRestoreOpsConfig:
    """Hooks after restoring the Metabase application database from a local dump."""

    envs: tuple[str, ...] | None
    db_psql: tuple[DbPsqlRestoreEntry, ...]
    manage_commands: tuple[tuple[str, ...], ...]
    restart_services: tuple[str, ...]

    def applies_to_env(self, env_name: str) -> bool:
        if self.envs is None:
            return True
        return env_name in self.envs


@dataclass(frozen=True)
class OpsConfig:
    install_prefix: str
    config_dir: str
    systemd_unit_prefix: str
    transfer_workdir: str
    pgbackrest: PgbackrestOpsConfig
    restic: ResticOpsConfig
    zabbix: ZabbixOpsConfig
    systemd_units: SystemdUnitsOpsConfig
    post_db_restore: PostDbRestoreOpsConfig
    post_metabase_db_restore: PostMetabaseDbRestoreOpsConfig
    default_db_container: str


VALID_FETCH_VIA = frozenset({"ssh_native", "ssh_docker", "dk", "script"})
REQUIRED_FETCH_DATABASE_KEYS = frozenset({"app"})


@dataclass(frozen=True)
class FetchDatabaseEntry:
    """One database source in ``native.fetch.databases``."""

    db_name: str
    via: str
    ssh_host: str | None = None
    container: str | None = None
    pg_user: str = "postgres"
    dk_env: str | None = None
    dump: str | None = None
    # Synthetic Docker defaults soft-skip when the remote DB is missing; explicit
    # tooling.yaml entries keep the hard-fail behaviour.
    soft_skip_if_missing: bool = False


DEFAULT_METABASE_DB_NAME = "metabase_db"
DEFAULT_METABASE_DUMP_FILENAME = "metabase_db.custom"


@dataclass(frozen=True)
class FetchConfig:
    """``native.fetch`` in tooling.yaml (config-driven ``dk fetch db``)."""

    dk_env: str
    ssh_host: str | None
    databases: dict[str, FetchDatabaseEntry]


@dataclass(frozen=True)
class FetchMediaLegacyConfig:
    """Fixed host directory for ``local fetch media --legacy-path`` (non-Docker deploys).

    When ``default`` is true, ``native fetch media`` uses legacy path mode unless
    ``--no-legacy-path`` is passed.
    """

    remote: str
    ssh_host: str | None
    default: bool = False


@dataclass(frozen=True)
class FetchMetabaseDbConfig:
    """``native.fetch_metabase_db`` in tooling.yaml (optional Metabase dump fetch defaults)."""

    ssh_host: str | None


@dataclass(frozen=True)
class FetchMediaConfig:
    """``native.fetch_media`` in tooling.yaml (defaults when the section is omitted)."""

    dk_env: str
    dest: str
    legacy: FetchMediaLegacyConfig | None


DEFAULT_DB_NAME_ENV_KEYS: tuple[str, ...] = (
    "DJANGO_DB_NAME",
    "DJANGO_DB",
    "POSTGRES_DB",
)
DEFAULT_DB_HOST_ENV_KEYS: tuple[str, ...] = (
    "DJANGO_DB_HOST",
    "POSTGRES_HOST",
    "DATABASE_HOST",
)
DEFAULT_DB_PORT_ENV_KEYS: tuple[str, ...] = (
    "DJANGO_DB_PORT",
    "POSTGRES_PORT",
    "DATABASE_PORT",
)
DEFAULT_DB_USER_ENV_KEYS: tuple[str, ...] = (
    "DJANGO_DB_USER",
    "POSTGRES_USER",
    "DATABASE_USER",
)
DEFAULT_DB_PASSWORD_ENV_KEYS: tuple[str, ...] = (
    "DJANGO_DB_PASSWORD",
    "POSTGRES_PASSWORD",
    "DATABASE_PASSWORD",
)
DEFAULT_LOCAL_PG_HOST = "localhost"
DEFAULT_LOCAL_PG_PORT = "5432"
DEFAULT_PG_RESTORE_ARGS: tuple[str, ...] = ("--clean", "--if-exists")


def local_postgres_db_name(project_name: str) -> str:
    """Default database name for host ``local reset-db`` / Django on localhost."""
    return f"{project_name}_db"


def resolve_native_db_name(config: "ProjectConfig") -> str:
    """Database name when env vars from ``native.reset_db.db_name_env`` are unset.

    Uses ``db_name_fallback``, else the stem of ``paths.fetch_db_dump`` (e.g.
    ``catalpa_db.custom`` → ``catalpa_db``), else ``local_postgres_db_name``.
    """
    reset = config.native.reset_db
    if reset.db_name_fallback:
        return reset.db_name_fallback
    dump = config.fetch_db_dump_path
    if dump.suffix in (".custom", ".dump", ".backup"):
        stem = dump.stem
        if stem:
            return stem
    return local_postgres_db_name(config.meta.name)


@dataclass(frozen=True)
class ResetDbConfig:
    """``native.reset_db`` in tooling.yaml (host ``native reset-db`` / ``native pg-restore``)."""

    postgis: bool
    restore_as_super: bool
    pg_restore_args: tuple[str, ...]
    post_manage_commands: tuple[tuple[str, ...], ...]
    db_name_env: tuple[str, ...]
    db_name_fallback: str | None
    host_env: tuple[str, ...]
    port_env: tuple[str, ...]
    user_env: tuple[str, ...]
    password_env: tuple[str, ...]


VALID_PACKAGE_MANAGERS: frozenset[str] = frozenset({"npm", "yarn", "pnpm"})
DEFAULT_FRONTEND_DEV_SCRIPT = "dev"
DEFAULT_NATIVE_START_PORTS: tuple[int, ...] = (8000, 8080)


DEFAULT_TEST_GROUP = "test"
DEFAULT_TEST_FRONTEND_SCRIPT = "test"


@dataclass(frozen=True)
class TestConfig:
    """``native.test`` in tooling.yaml (how ``tests backend`` / ``tests frontend`` invoke)."""

    group: str
    frontend_script: str


@dataclass(frozen=True)
class StartConfig:
    """``native.start`` in tooling.yaml (host ``native start`` via Honcho)."""

    procfile: str | None
    ports: tuple[int, ...]
    migrate: bool


@dataclass(frozen=True)
class DjangoDevConfig:
    """``native.django`` in tooling.yaml (host ``native runserver`` bind port)."""

    port: int | None


@dataclass(frozen=True)
class FrontendDevConfig:
    """``native.frontend`` in tooling.yaml (host ``native frontend`` / ``native vite``)."""

    package_manager: str | None
    script: str
    install: bool
    env: dict[str, str]
    node_version: str | None


@dataclass(frozen=True)
class NativeConfig:
    fetch: FetchConfig
    fetch_media: FetchMediaConfig
    fetch_metabase_db: FetchMetabaseDbConfig
    reset_db: ResetDbConfig
    django: DjangoDevConfig
    frontend: FrontendDevConfig
    start: StartConfig
    test: TestConfig


@dataclass(frozen=True)
class DevConfig:
    """Optional ``dev:`` section in tooling.yaml (local dev defaults)."""

    site_origin_base: str
    lan_dns_suffix: str
    build_time_zone: str


DEFAULT_FORBIDDEN_SPDX: tuple[str, ...] = (
    "UNLICENSED",
    "LicenseRef-proprietary",
)
DEFAULT_WARN_SPDX: tuple[str, ...] = (
    "GPL-2.0-only",
    "GPL-3.0-only",
    "UNKNOWN",
)


@dataclass(frozen=True)
class CompliancePythonConfig:
    lockfiles: tuple[str, ...]


@dataclass(frozen=True)
class ComplianceJavascriptConfig:
    cwd: str | None
    lockfile: str
    production_only: bool


@dataclass(frozen=True)
class ComplianceBundledAssetConfig:
    path: str
    license_globs: tuple[str, ...]


@dataclass(frozen=True)
class ComplianceOutputsConfig:
    sbom_dir: str
    notices: str


@dataclass(frozen=True)
class ComplianceConfig:
    project_license: str
    license_files: tuple[str, ...]
    python: CompliancePythonConfig | None
    javascript: ComplianceJavascriptConfig | None
    bundled_assets: tuple[ComplianceBundledAssetConfig, ...]
    forbidden_spdx: tuple[str, ...]
    warn_spdx: tuple[str, ...]
    allow_strong_copyleft: bool
    outputs: ComplianceOutputsConfig
    allowed_packages: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectMetaConfig:
    name: str
    root_marker: str


@dataclass(frozen=True)
class SpacesConfig:
    """DigitalOcean Spaces defaults for backup auto-provisioning (``digitalocean.spaces``)."""

    endpoint: str | None
    region: str | None
    bucket: str | None
    pgbackrest_repo_path: str | None
    restic_path: str | None
    stanza: str | None


@dataclass(frozen=True)
class DigitalOceanConfig:
    project_name: str | None
    project_id: str | None
    context: str | None
    timezone: str | None
    region: str | None
    size: str | None
    image: str | None
    ssh_keys: tuple[str, ...] | None
    spaces: SpacesConfig | None = None
    monitoring: bool = True


DEFAULT_FETCH_MEDIA_DK_ENV = "prod"
DEFAULT_FETCH_MEDIA_DEST = "media"


@dataclass(frozen=True)
class ProjectConfig:
    """Typed view of ``tooling.yaml`` with resolved paths under ``repo_root``."""

    meta: ProjectMetaConfig
    paths: PathsConfig
    stack_optional: StackConfig | None
    ops_optional: OpsConfig | None
    native: NativeConfig
    dev: DevConfig
    digitalocean: DigitalOceanConfig | None
    compliance: ComplianceConfig | None
    repo_root: Path
    tooling_path: Path

    @property
    def stack(self) -> StackConfig:
        """Compose stack config, or raise when ``stack:`` is absent."""
        if self.stack_optional is None:
            raise _missing_section(
                "stack",
                needed_for="stack commands (dk build, dk push, dk <env>, tests ci)",
            )
        return self.stack_optional

    @property
    def ops(self) -> OpsConfig:
        """Host ops config, or raise when ``ops:`` is absent."""
        if self.ops_optional is None:
            raise _missing_section(
                "ops",
                needed_for="host ops commands (dk transfer, backups, systemd, zabbix)",
            )
        return self.ops_optional

    @property
    def has_stack(self) -> bool:
        """True when the manifest declares a ``stack:`` section."""
        return self.stack_optional is not None

    @property
    def has_ops(self) -> bool:
        """True when the manifest declares an ``ops:`` section."""
        return self.ops_optional is not None

    @property
    def has_deploy_paths(self) -> bool:
        """True when the manifest declares a ``paths.deploy`` section."""
        return self.paths.deploy_optional is not None

    @property
    def backend_dir(self) -> Path:
        return self.repo_root / self.paths.backend

    @property
    def frontend_dir(self) -> Path:
        return self.repo_root / self.paths.frontend

    @property
    def prototype_dir(self) -> Path | None:
        if self.paths.prototype is None:
            return None
        return self.repo_root / self.paths.prototype

    @property
    def scripts_dir(self) -> Path:
        """Primary scripts directory (first ``paths.scripts`` entry)."""
        return self.repo_root / self.paths.scripts[0]

    @property
    def scripts_dirs(self) -> tuple[Path, ...]:
        """All configured script directories (``paths.scripts`` string or list)."""
        return tuple(self.repo_root / rel for rel in self.paths.scripts)

    @property
    def env_local_path(self) -> Path:
        return self.repo_root / self.paths.env_local

    @property
    def email_backend_dir(self) -> Path:
        return self.repo_root / self.paths.email_backend_dir

    @property
    def media_dir(self) -> Path | None:
        if self.paths.media_dir is None:
            return None
        return self.repo_root / self.paths.media_dir

    @property
    def fetch_db_dump_path(self) -> Path:
        return self.repo_root / self.paths.fetch_db_dump

    @property
    def fetch_metabase_db_dump_path(self) -> Path:
        """Metabase dump path: ``paths.fetch_metabase_db_dump`` or sibling of the app dump."""
        rel = self.paths.fetch_metabase_db_dump
        if rel is None:
            rel = str(Path(self.paths.fetch_db_dump).with_name(DEFAULT_METABASE_DUMP_FILENAME))
        return self.repo_root / rel

    @property
    def fetch_media_dest_path(self) -> Path:
        return self.repo_root / self.native.fetch_media.dest

    @property
    def default_fetch_dk_env(self) -> str:
        """Default ``docker/envs/<name>`` for ``dk fetch`` / ``native fetch``."""
        return self.native.fetch.dk_env

    def fetch_database_output_path(self, key: str, entry: FetchDatabaseEntry) -> Path | None:
        """Resolve local dump path for a configured fetch database key."""
        if entry.dump:
            return self.repo_root / entry.dump
        if key == "app":
            return self.fetch_db_dump_path
        if key == "metabase":
            return self.fetch_metabase_db_dump_path
        return None

    def has_metabase_fetch(self) -> bool:
        """True when Metabase is configured under ``native.fetch.databases``."""
        return "metabase" in self.native.fetch.databases

    @property
    def deploy_envs_dir(self) -> Path:
        return self.repo_root / self.paths.deploy.envs_dir

    @property
    def images_config_path(self) -> Path:
        return self.repo_root / self.paths.deploy.images_config

    @property
    def compose_prod(self) -> str:
        return self.paths.deploy.default_compose

    @property
    def compose_dev(self) -> str:
        return self.paths.deploy.dev_compose

    def image_component(self, key: str) -> str:
        comp = self.stack.images.components.get(key)
        if not comp:
            raise ProjectConfigError(f"Missing stack.images.components[{key!r}]")
        return comp

    def stack_service(self, role: str) -> str:
        """Resolve compose service name by role (web, proxy, db)."""
        svc = getattr(self.stack.services, role, None)
        if not svc:
            raise ProjectConfigError(f"Missing stack.services.{role}")
        return svc

    def credentials_optional_for_env(self, env_name: str) -> bool:
        canonical = self.resolve_deploy_env_name(env_name)
        if canonical in self.paths.deploy.credentials_optional_envs:
            return True
        return canonical.startswith("local_")

    def resolve_deploy_env_name(self, env_name: str) -> str:
        """Return canonical env name (follows ``paths.deploy.env_aliases``)."""
        return self.paths.deploy.env_aliases.get(env_name, env_name)

    @classmethod
    def from_cwd(cls) -> ProjectConfig:
        return load_project_config(repo_root_from_cwd())


def _parse_string_list(raw: Any, *, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProjectConfigError(f"{field} must be a list")
    out: list[str] = []
    for item in raw:
        s = str(item).strip()
        if not s:
            raise ProjectConfigError(f"Empty entry in {field}")
        out.append(s)
    return tuple(out)


def _parse_manage_commands_list(raw: Any, *, field_prefix: str) -> tuple[tuple[str, ...], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProjectConfigError(f"{field_prefix} must be a list")
    commands: list[tuple[str, ...]] = []
    for i, item in enumerate(raw):
        field = f"{field_prefix}[{i}]"
        if isinstance(item, str):
            argv = tuple(shlex.split(item))
        elif isinstance(item, list):
            argv_list: list[str] = []
            for tok in item:
                if tok is None or isinstance(tok, bool):
                    raise ProjectConfigError(f"{field}: argv token must be a string")
                s = str(tok).strip()
                if not s:
                    raise ProjectConfigError(f"Empty argv token in {field}")
                argv_list.append(s)
            argv = tuple(argv_list)
        else:
            raise ProjectConfigError(f"{field} must be a string or list of strings")
        if not argv:
            raise ProjectConfigError(f"Empty command in {field}")
        commands.append(argv)
    return tuple(commands)


def _parse_env_key_list(raw: Any, *, field: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None:
        return default
    return _parse_string_list(raw, field=field)


def _parse_db_psql_list(
    raw: Any,
    *,
    field_prefix: str,
    allowed_targets: frozenset[str],
) -> tuple[DbPsqlRestoreEntry, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProjectConfigError(f"{field_prefix} must be a list")
    entries: list[DbPsqlRestoreEntry] = []
    for i, item in enumerate(raw):
        loc = f"{field_prefix}[{i}]"
        if not isinstance(item, dict):
            raise ProjectConfigError(f"{loc} must be a mapping")
        target = item.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ProjectConfigError(f"{loc}.target must be a non-empty string")
        target = target.strip()
        if target not in allowed_targets:
            allowed = ", ".join(sorted(allowed_targets))
            raise ProjectConfigError(
                f"{loc}.target must be one of: {allowed} (got {target!r})"
            )
        file_raw = item.get("file")
        if not isinstance(file_raw, str) or not file_raw.strip():
            raise ProjectConfigError(f"{loc}.file must be a non-empty string")
        entries.append(DbPsqlRestoreEntry(target=target, file=file_raw.strip()))
    return tuple(entries)


def _parse_post_metabase_db_restore(raw: Any) -> PostMetabaseDbRestoreOpsConfig:
    if raw is None:
        return PostMetabaseDbRestoreOpsConfig(
            envs=None,
            db_psql=(),
            manage_commands=(),
            restart_services=(),
        )
    if not isinstance(raw, dict):
        raise ProjectConfigError("ops.post_metabase_db_restore must be a mapping")
    envs_raw = raw.get("envs")
    envs: tuple[str, ...] | None = None
    if envs_raw is not None:
        envs = _parse_string_list(envs_raw, field="ops.post_metabase_db_restore.envs")
    db_psql = _parse_db_psql_list(
        raw.get("db_psql"),
        field_prefix="ops.post_metabase_db_restore.db_psql",
        allowed_targets=frozenset({"metabase"}),
    )
    commands = _parse_manage_commands_list(
        raw.get("manage_commands"),
        field_prefix="ops.post_metabase_db_restore.manage_commands",
    )
    restart_services = _parse_string_list(
        raw.get("restart_services"),
        field="ops.post_metabase_db_restore.restart_services",
    )
    return PostMetabaseDbRestoreOpsConfig(
        envs=envs,
        db_psql=db_psql,
        manage_commands=commands,
        restart_services=restart_services,
    )


def _parse_post_db_restore(raw: Any) -> PostDbRestoreOpsConfig:
    if raw is None:
        return PostDbRestoreOpsConfig(envs=None, db_psql=(), manage_commands=())
    if not isinstance(raw, dict):
        raise ProjectConfigError("ops.post_db_restore must be a mapping")
    envs_raw = raw.get("envs")
    envs: tuple[str, ...] | None = None
    if envs_raw is not None:
        envs = _parse_string_list(envs_raw, field="ops.post_db_restore.envs")
    db_psql = _parse_db_psql_list(
        raw.get("db_psql"),
        field_prefix="ops.post_db_restore.db_psql",
        allowed_targets=frozenset({"app"}),
    )
    commands = _parse_manage_commands_list(
        raw.get("manage_commands"),
        field_prefix="ops.post_db_restore.manage_commands",
    )
    return PostDbRestoreOpsConfig(
        envs=envs,
        db_psql=db_psql,
        manage_commands=commands,
    )


def _parse_reset_db(raw: Any) -> ResetDbConfig:
    if raw is None:
        return ResetDbConfig(
            postgis=False,
            restore_as_super=False,
            pg_restore_args=DEFAULT_PG_RESTORE_ARGS,
            post_manage_commands=(),
            db_name_env=DEFAULT_DB_NAME_ENV_KEYS,
            db_name_fallback=None,
            host_env=DEFAULT_DB_HOST_ENV_KEYS,
            port_env=DEFAULT_DB_PORT_ENV_KEYS,
            user_env=DEFAULT_DB_USER_ENV_KEYS,
            password_env=DEFAULT_DB_PASSWORD_ENV_KEYS,
        )
    if not isinstance(raw, dict):
        raise ProjectConfigError("native.reset_db must be a mapping")
    fallback = _optional_str(raw, "db_name_fallback")
    postgis = _optional_bool(raw, "postgis", default=False)
    return ResetDbConfig(
        postgis=postgis,
        restore_as_super=_optional_bool(raw, "restore_as_super", default=False),
        pg_restore_args=(
            _parse_string_list(
                raw.get("pg_restore_args"),
                field="native.reset_db.pg_restore_args",
            )
            if raw.get("pg_restore_args") is not None
            else DEFAULT_PG_RESTORE_ARGS
        ),
        post_manage_commands=_parse_manage_commands_list(
            raw.get("post_manage_commands"),
            field_prefix="native.reset_db.post_manage_commands",
        ),
        db_name_env=_parse_env_key_list(
            raw.get("db_name_env"),
            field="native.reset_db.db_name_env",
            default=DEFAULT_DB_NAME_ENV_KEYS,
        ),
        db_name_fallback=fallback,
        host_env=_parse_env_key_list(
            raw.get("host_env"),
            field="native.reset_db.host_env",
            default=DEFAULT_DB_HOST_ENV_KEYS,
        ),
        port_env=_parse_env_key_list(
            raw.get("port_env"),
            field="native.reset_db.port_env",
            default=DEFAULT_DB_PORT_ENV_KEYS,
        ),
        user_env=_parse_env_key_list(
            raw.get("user_env"),
            field="native.reset_db.user_env",
            default=DEFAULT_DB_USER_ENV_KEYS,
        ),
        password_env=_parse_env_key_list(
            raw.get("password_env"),
            field="native.reset_db.password_env",
            default=DEFAULT_DB_PASSWORD_ENV_KEYS,
        ),
    )


def _parse_env_aliases(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProjectConfigError("paths.deploy.env_aliases must be a mapping")
    out: dict[str, str] = {}
    for key, val in raw.items():
        alias = str(key).strip()
        target = str(val).strip()
        if not alias or not target:
            raise ProjectConfigError("paths.deploy.env_aliases keys and values must be non-empty strings")
        if alias == target:
            raise ProjectConfigError(f"paths.deploy.env_aliases: {alias!r} must not map to itself")
        out[alias] = target
    return out


def _parse_scripts_paths(raw: Any) -> tuple[str, ...]:
    """``paths.scripts``: single directory or ordered list (first wins on name clash)."""
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise ProjectConfigError("paths.scripts must not be empty")
        return (_validate_rel_path(s, field="paths.scripts"),)
    if isinstance(raw, list):
        if not raw:
            raise ProjectConfigError("paths.scripts list must not be empty")
        out: list[str] = []
        for i, item in enumerate(raw):
            s = str(item).strip()
            if not s:
                raise ProjectConfigError(f"Empty entry in paths.scripts[{i}]")
            out.append(_validate_rel_path(s, field=f"paths.scripts[{i}]"))
        return tuple(out)
    raise ProjectConfigError("paths.scripts must be a string or list of strings")


def _db_name_from_dump_path(dump_rel: str, *, project_name: str) -> str:
    stem = Path(dump_rel).stem
    if stem:
        return stem
    return local_postgres_db_name(project_name)


def _env_info_has_ssh_docker_host(
    repo_root: Path,
    paths: PathsConfig,
    dk_env: str,
) -> bool:
    """True when ``docker/envs/<dk_env>/info.yaml`` has an SSH ``docker_host``."""
    if paths.deploy_optional is None:
        return False
    info_path = repo_root / paths.deploy_optional.envs_dir / dk_env / "info.yaml"
    if not info_path.is_file():
        return False
    try:
        with open(info_path, encoding="utf-8") as f:
            info = yaml.safe_load(f) or {}
    except OSError:
        return False
    if not isinstance(info, dict):
        return False
    docker_host = str(info.get("docker_host") or "").strip()
    return docker_host.lower().startswith("ssh://")


def _default_fetch_config(
    *,
    paths: PathsConfig,
    fetch_media: FetchMediaConfig,
    fetch_metabase_db: FetchMetabaseDbConfig,
    repo_root: Path,
    project_name: str,
    dk_env: str | None = None,
) -> FetchConfig:
    """Synthetic ``native.fetch`` when ``native.fetch.databases`` is omitted."""
    env_name = (dk_env or fetch_media.dk_env or DEFAULT_FETCH_MEDIA_DK_ENV).strip()
    ssh_host = fetch_metabase_db.ssh_host
    if not ssh_host and fetch_media.legacy and fetch_media.legacy.ssh_host:
        ssh_host = fetch_media.legacy.ssh_host
    scripts_dir = repo_root / paths.scripts[0] if paths.scripts else repo_root / "scripts"
    app_db = _db_name_from_dump_path(paths.fetch_db_dump, project_name=project_name)

    if _env_info_has_ssh_docker_host(repo_root, paths, env_name):
        databases: dict[str, FetchDatabaseEntry] = {
            "app": FetchDatabaseEntry(db_name=app_db, via="dk"),
            "metabase": FetchDatabaseEntry(
                db_name=DEFAULT_METABASE_DB_NAME,
                via="dk",
                soft_skip_if_missing=True,
            ),
        }
        return FetchConfig(dk_env=env_name, ssh_host=ssh_host, databases=databases)

    via = "script" if (scripts_dir / "fetch_db.sh").is_file() else "ssh_native"
    databases = {
        "app": FetchDatabaseEntry(db_name=app_db, via=via, ssh_host=ssh_host),
    }
    if ssh_host:
        databases["metabase"] = FetchDatabaseEntry(
            db_name=DEFAULT_METABASE_DB_NAME,
            via="ssh_native",
            ssh_host=ssh_host,
            soft_skip_if_missing=True,
        )
    return FetchConfig(
        dk_env=env_name,
        ssh_host=ssh_host,
        databases=databases,
    )


def _parse_fetch_database_entry(raw: Any, *, key: str) -> FetchDatabaseEntry:
    if not isinstance(raw, dict):
        raise ProjectConfigError(f"native.fetch.databases.{key} must be a mapping")
    db_name = _require_str(raw, "db_name", section=f"native.fetch.databases.{key}")
    via = _require_str(raw, "via", section=f"native.fetch.databases.{key}")
    if via not in VALID_FETCH_VIA:
        raise ProjectConfigError(
            f"native.fetch.databases.{key}.via must be one of "
            f"{', '.join(sorted(VALID_FETCH_VIA))} (got {via!r})"
        )
    container = _optional_str(raw, "container")
    if via == "ssh_docker" and not container:
        raise ProjectConfigError(
            f"native.fetch.databases.{key}.container is required when via is ssh_docker"
        )
    ssh_host = _optional_str(raw, "ssh_host")
    pg_user = _optional_str(raw, "pg_user") or "postgres"
    dk_env = _optional_str(raw, "dk_env")
    dump = (
        _validate_rel_path(p, field=f"native.fetch.databases.{key}.dump")
        if (p := _optional_str(raw, "dump"))
        else None
    )
    return FetchDatabaseEntry(
        db_name=db_name,
        via=via,
        ssh_host=ssh_host,
        container=container,
        pg_user=pg_user,
        dk_env=dk_env,
        dump=dump,
    )


def _parse_fetch_databases(raw: Any) -> dict[str, FetchDatabaseEntry]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProjectConfigError("native.fetch.databases must be a mapping")
    if not raw:
        raise ProjectConfigError("native.fetch.databases must not be empty when set")
    if "app" not in raw:
        raise ProjectConfigError("native.fetch.databases must include an `app` entry")
    out: dict[str, FetchDatabaseEntry] = {}
    for key, value in raw.items():
        k = str(key).strip()
        if not k:
            raise ProjectConfigError("native.fetch.databases keys must be non-empty strings")
        if k in out:
            raise ProjectConfigError(f"duplicate native.fetch.databases key: {k!r}")
        out[k] = _parse_fetch_database_entry(value, key=k)
    return out


def _parse_fetch(
    raw: Any,
    *,
    paths: PathsConfig,
    fetch_media: FetchMediaConfig,
    fetch_metabase_db: FetchMetabaseDbConfig,
    repo_root: Path,
    project_name: str,
) -> FetchConfig:
    if raw is None:
        if fetch_metabase_db.ssh_host:
            warn_deprecated(
                "native.fetch_metabase_db",
                "native.fetch.databases.metabase",
                context="tooling.yaml",
            )
        return _default_fetch_config(
            paths=paths,
            fetch_media=fetch_media,
            fetch_metabase_db=fetch_metabase_db,
            repo_root=repo_root,
            project_name=project_name,
            dk_env=fetch_media.dk_env,
        )
    if not isinstance(raw, dict):
        raise ProjectConfigError("native.fetch must be a mapping")
    if fetch_metabase_db.ssh_host:
        warn_deprecated(
            "native.fetch_metabase_db",
            "native.fetch.databases.metabase",
            context="tooling.yaml `native.fetch_metabase_db` is ignored when `native.fetch` is set",
        )
    dk_env = _optional_str(raw, "dk_env") or fetch_media.dk_env
    ssh_host = _optional_str(raw, "ssh_host")
    if not ssh_host:
        ssh_host = fetch_metabase_db.ssh_host
    if not ssh_host and fetch_media.legacy and fetch_media.legacy.ssh_host:
        ssh_host = fetch_media.legacy.ssh_host
    databases = _parse_fetch_databases(raw.get("databases"))
    if not databases:
        return _default_fetch_config(
            paths=paths,
            fetch_media=fetch_media,
            fetch_metabase_db=fetch_metabase_db,
            repo_root=repo_root,
            project_name=project_name,
            dk_env=dk_env,
        )
    return FetchConfig(dk_env=dk_env, ssh_host=ssh_host, databases=databases)


def _parse_fetch_metabase_db(raw: Any) -> FetchMetabaseDbConfig:
    if raw is None:
        return FetchMetabaseDbConfig(ssh_host=None)
    if not isinstance(raw, dict):
        raise ProjectConfigError("native.fetch_metabase_db must be a mapping")
    ssh_host = _optional_str(raw, "ssh_host")
    return FetchMetabaseDbConfig(ssh_host=ssh_host)


def _parse_deploy_paths(deploy_raw: dict[str, Any]) -> DeployPathsConfig:
    return DeployPathsConfig(
        envs_dir=_validate_rel_path(
            _optional_str(deploy_raw, "envs_dir") or DEFAULT_DEPLOY_ENVS_DIR,
            field="paths.deploy.envs_dir",
        ),
        images_config=_validate_rel_path(
            _optional_str(deploy_raw, "images_config") or DEFAULT_DEPLOY_IMAGES_CONFIG,
            field="paths.deploy.images_config",
        ),
        default_compose=_validate_rel_path(
            _optional_str(deploy_raw, "default_compose") or DEFAULT_DEPLOY_COMPOSE,
            field="paths.deploy.default_compose",
        ),
        dev_compose=_validate_rel_path(
            _optional_str(deploy_raw, "dev_compose") or DEFAULT_DEPLOY_DEV_COMPOSE,
            field="paths.deploy.dev_compose",
        ),
        credentials_optional_envs=_parse_string_list(
            deploy_raw.get("credentials_optional_envs"),
            field="paths.deploy.credentials_optional_envs",
        ),
        env_aliases=_parse_env_aliases(deploy_raw.get("env_aliases")),
    )


def _parse_paths(paths_raw: dict[str, Any], *, project_name: str) -> PathsConfig:
    deploy_raw = paths_raw.get("deploy")
    deploy = (
        _parse_deploy_paths(_require_mapping(deploy_raw, "paths.deploy"))
        if deploy_raw is not None
        else None
    )
    fetch_db = _optional_str(paths_raw, "fetch_db_dump") or default_fetch_db_dump_path(project_name)
    return PathsConfig(
        backend=_validate_rel_path(
            _require_str(paths_raw, "backend", section="paths"), field="paths.backend"
        ),
        frontend=_validate_rel_path(
            _require_str(paths_raw, "frontend", section="paths"), field="paths.frontend"
        ),
        prototype=(
            _validate_rel_path(p, field="paths.prototype")
            if (p := _optional_str(paths_raw, "prototype"))
            else None
        ),
        scripts=_parse_scripts_paths(paths_raw.get("scripts")),
        env_local=_validate_rel_path(
            _require_str(paths_raw, "env_local", section="paths"), field="paths.env_local"
        ),
        email_backend_dir=_validate_rel_path(
            _require_str(paths_raw, "email_backend_dir", section="paths"),
            field="paths.email_backend_dir",
        ),
        media_dir=(
            _validate_rel_path(p, field="paths.media_dir")
            if (p := _optional_str(paths_raw, "media_dir"))
            else None
        ),
        fetch_db_dump=_validate_rel_path(fetch_db, field="paths.fetch_db_dump"),
        fetch_metabase_db_dump=(
            _validate_rel_path(p, field="paths.fetch_metabase_db_dump")
            if (p := _optional_str(paths_raw, "fetch_metabase_db_dump"))
            else None
        ),
        deploy_optional=deploy,
    )


def _parse_build_placeholders(raw: Any) -> dict[str, str]:
    if raw is None:
        return dict(DEFAULT_BUILD_PLACEHOLDERS)
    if not isinstance(raw, dict):
        raise ProjectConfigError("stack.build_placeholders must be a mapping")
    out = dict(DEFAULT_BUILD_PLACEHOLDERS)
    for key, value in raw.items():
        k = str(key).strip()
        if not k:
            raise ProjectConfigError("stack.build_placeholders keys must be non-empty strings")
        out[k] = str(value)
    return out


def _parse_dev(raw: Any, *, digitalocean: DigitalOceanConfig | None) -> DevConfig:
    tz_default = DEFAULT_BUILD_TIME_ZONE
    if digitalocean is not None and digitalocean.timezone:
        tz_default = digitalocean.timezone
    if raw is None:
        return DevConfig(
            site_origin_base=DEFAULT_DEV_SITE_ORIGIN_BASE,
            lan_dns_suffix=DEFAULT_DEV_LAN_DNS_SUFFIX,
            build_time_zone=tz_default,
        )
    if not isinstance(raw, dict):
        raise ProjectConfigError("dev must be a mapping")
    site_base = _optional_str(raw, "site_origin_base") or DEFAULT_DEV_SITE_ORIGIN_BASE
    lan_suffix = _optional_str(raw, "lan_dns_suffix") or DEFAULT_DEV_LAN_DNS_SUFFIX
    build_tz = _optional_str(raw, "build_time_zone") or tz_default
    return DevConfig(
        site_origin_base=site_base,
        lan_dns_suffix=lan_suffix,
        build_time_zone=build_tz,
    )


def _parse_stack(stack_raw: dict[str, Any], *, project_name: str) -> StackConfig:
    services_raw = stack_raw.get("services")
    if services_raw is None:
        services = StackServicesConfig(
            web=DEFAULT_STACK_WEB_SERVICE,
            proxy=DEFAULT_STACK_PROXY_SERVICE,
            db=DEFAULT_STACK_DB_SERVICE,
        )
    else:
        services_map = _require_mapping(services_raw, "stack.services")
        services = StackServicesConfig(
            web=_optional_str(services_map, "web") or DEFAULT_STACK_WEB_SERVICE,
            proxy=_optional_str(services_map, "proxy") or DEFAULT_STACK_PROXY_SERVICE,
            db=_optional_str(services_map, "db") or DEFAULT_STACK_DB_SERVICE,
        )

    images_raw = stack_raw.get("images")
    if images_raw is None:
        images_raw = {}
    elif not isinstance(images_raw, dict):
        raise ProjectConfigError("stack.images must be a mapping")
    components_raw = images_raw.get("components")
    if components_raw is None:
        components = default_stack_image_components(project_name)
    else:
        components_map = _require_mapping(components_raw, "stack.images.components")
        defaults = default_stack_image_components(project_name)
        components = {
            "web": _optional_str(components_map, "web") or defaults["web"],
            "proxy": _optional_str(components_map, "proxy") or defaults["proxy"],
            "db": _optional_str(components_map, "db") or defaults["db"],
        }
        for key, val in components_map.items():
            k = str(key)
            if k not in components:
                components[k] = _require_str({k: val}, k, section="stack.images.components")

    health_raw = _require_mapping(stack_raw.get("healthcheck"), "stack.healthcheck")
    health_service = _optional_str(health_raw, "service") or services.web
    origin_keys = _parse_env_key_list(
        stack_raw.get("origin_env_keys"),
        field="stack.origin_env_keys",
        default=DEFAULT_ORIGIN_ENV_KEYS,
    )
    return StackConfig(
        compose_project_default=(
            _optional_str(stack_raw, "compose_project_default") or project_name
        ),
        services=services,
        images=StackImagesConfig(
            registry_key=(
                _optional_str(images_raw, "registry_key") or DEFAULT_STACK_REGISTRY_KEY
            ),
            components=components,
        ),
        healthcheck=StackHealthcheckConfig(
            service=health_service,
            url=_require_str(health_raw, "url", section="stack.healthcheck"),
        ),
        origin_env_keys=origin_keys,
        build_placeholders=_parse_build_placeholders(stack_raw.get("build_placeholders")),
    )


def _parse_spaces(spaces_raw: dict[str, Any]) -> SpacesConfig:
    return SpacesConfig(
        endpoint=_optional_str(spaces_raw, "endpoint"),
        region=_optional_str(spaces_raw, "region"),
        bucket=_optional_str(spaces_raw, "bucket"),
        pgbackrest_repo_path=_optional_str(spaces_raw, "pgbackrest_repo_path"),
        restic_path=_optional_str(spaces_raw, "restic_path"),
        stanza=_optional_str(spaces_raw, "stanza"),
    )


def _parse_fetch_media_legacy(raw: Any) -> FetchMediaLegacyConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProjectConfigError("native.fetch_media.legacy must be a mapping")
    remote = _require_str(raw, "remote", section="native.fetch_media.legacy")
    ssh_host = _optional_str(raw, "ssh_host")
    use_legacy = _optional_bool(raw, "default", default=False)
    return FetchMediaLegacyConfig(
        remote=remote.rstrip("/"),
        ssh_host=ssh_host,
        default=use_legacy,
    )


def _parse_fetch_media(raw: Any) -> FetchMediaConfig:
    if raw is None:
        return FetchMediaConfig(
            dk_env=DEFAULT_FETCH_MEDIA_DK_ENV,
            dest=DEFAULT_FETCH_MEDIA_DEST,
            legacy=None,
        )
    if not isinstance(raw, dict):
        raise ProjectConfigError("native.fetch_media must be a mapping")
    dest = _optional_str(raw, "dest") or DEFAULT_FETCH_MEDIA_DEST
    dk_env = _optional_str(raw, "dk_env") or DEFAULT_FETCH_MEDIA_DK_ENV
    return FetchMediaConfig(
        dk_env=dk_env,
        dest=_validate_rel_path(dest, field="native.fetch_media.dest"),
        legacy=_parse_fetch_media_legacy(raw.get("legacy")),
    )


def _parse_frontend_env(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProjectConfigError("native.frontend.env must be a mapping")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        k = str(key).strip()
        if not k:
            raise ProjectConfigError("native.frontend.env keys must be non-empty strings")
        out[k] = str(value)
    return out


def _parse_django_port(raw: Any) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProjectConfigError("native.django must be a mapping")
    port = raw.get("port")
    if port is None:
        return None
    if not isinstance(port, int) or port < 1 or port > 65535:
        raise ProjectConfigError(
            f"native.django.port must be an integer from 1 to 65535 (got {port!r})"
        )
    return port


def _parse_django(raw: Any) -> DjangoDevConfig:
    if raw is None:
        return DjangoDevConfig(port=None)
    return DjangoDevConfig(port=_parse_django_port(raw))


def native_runserver_bind(django: DjangoDevConfig) -> str | None:
    """Return ``0.0.0.0:<port>`` when ``native.django.port`` is configured."""
    if django.port is None:
        return None
    return f"0.0.0.0:{django.port}"


def _parse_start_ports(raw: Any) -> tuple[int, ...]:
    if raw is None:
        return DEFAULT_NATIVE_START_PORTS
    if not isinstance(raw, list):
        raise ProjectConfigError("native.start.ports must be a list of integers")
    ports: list[int] = []
    for item in raw:
        if not isinstance(item, int) or item < 1 or item > 65535:
            raise ProjectConfigError(
                f"native.start.ports entries must be integers from 1 to 65535 (got {item!r})"
            )
        ports.append(item)
    return tuple(ports) if ports else DEFAULT_NATIVE_START_PORTS


def _parse_test(raw: Any) -> TestConfig:
    """``native.test`` — the uv dependency group and the package.json script to run."""
    if raw is None:
        return TestConfig(group=DEFAULT_TEST_GROUP, frontend_script=DEFAULT_TEST_FRONTEND_SCRIPT)
    if not isinstance(raw, dict):
        raise ProjectConfigError("native.test must be a mapping")
    return TestConfig(
        group=_optional_str(raw, "group") or DEFAULT_TEST_GROUP,
        frontend_script=_optional_str(raw, "frontend_script") or DEFAULT_TEST_FRONTEND_SCRIPT,
    )


def _parse_start(raw: Any) -> StartConfig:
    if raw is None:
        return StartConfig(
            procfile=None,
            ports=DEFAULT_NATIVE_START_PORTS,
            migrate=True,
        )
    if not isinstance(raw, dict):
        raise ProjectConfigError("native.start must be a mapping")
    procfile = _optional_str(raw, "procfile")
    if procfile is not None:
        procfile = _validate_rel_path(procfile, field="native.start.procfile")
    ports = _parse_start_ports(raw.get("ports"))
    migrate = _optional_bool(raw, "migrate", default=True)
    return StartConfig(procfile=procfile, ports=ports, migrate=migrate)


def _parse_frontend(raw: Any) -> FrontendDevConfig:
    if raw is None:
        return FrontendDevConfig(
            package_manager=None,
            script=DEFAULT_FRONTEND_DEV_SCRIPT,
            install=True,
            env={},
            node_version=None,
        )
    if not isinstance(raw, dict):
        raise ProjectConfigError("native.frontend must be a mapping")
    package_manager = _optional_str(raw, "package_manager")
    if package_manager is not None:
        package_manager = package_manager.lower()
        if package_manager not in VALID_PACKAGE_MANAGERS:
            raise ProjectConfigError(
                f"native.frontend.package_manager must be one of "
                f"{', '.join(sorted(VALID_PACKAGE_MANAGERS))} (got {package_manager!r})"
            )
    script = _optional_str(raw, "script") or DEFAULT_FRONTEND_DEV_SCRIPT
    install = _optional_bool(raw, "install", default=True)
    env = _parse_frontend_env(raw.get("env"))
    node_version = _optional_str(raw, "node_version")
    return FrontendDevConfig(
        package_manager=package_manager,
        script=script,
        install=install,
        env=env,
        node_version=node_version,
    )


def resolve_frontend_package_manager(frontend_dir: Path, *, configured: str | None) -> str:
    """Resolve npm/yarn/pnpm for ``native frontend`` (explicit config, then auto-detect)."""
    if configured is not None:
        return configured
    pkg_json = frontend_dir / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        pm_field = str(data.get("packageManager", "")).split("@", 1)[0].strip().lower()
        if pm_field in VALID_PACKAGE_MANAGERS:
            return pm_field
    if (frontend_dir / ".yarnrc.yml").is_file() or (frontend_dir / "yarn.lock").is_file():
        return "yarn"
    if (frontend_dir / "pnpm-lock.yaml").is_file():
        return "pnpm"
    return "npm"


def _parse_native(
    raw: Any,
    *,
    paths: PathsConfig,
    repo_root: Path,
    project_name: str,
) -> NativeConfig:
    fetch_media = _parse_fetch_media(None if raw is None else raw.get("fetch_media"))
    fetch_metabase_db = _parse_fetch_metabase_db(None if raw is None else raw.get("fetch_metabase_db"))
    fetch = _parse_fetch(
        None if raw is None else raw.get("fetch"),
        paths=paths,
        fetch_media=fetch_media,
        fetch_metabase_db=fetch_metabase_db,
        repo_root=repo_root,
        project_name=project_name,
    )
    if raw is None:
        return NativeConfig(
            fetch=fetch,
            fetch_media=fetch_media,
            fetch_metabase_db=fetch_metabase_db,
            reset_db=_parse_reset_db(None),
            django=_parse_django(None),
            frontend=_parse_frontend(None),
            start=_parse_start(None),
            test=_parse_test(None),
        )
    if not isinstance(raw, dict):
        raise ProjectConfigError("native must be a mapping")
    return NativeConfig(
        fetch=fetch,
        fetch_media=_parse_fetch_media(raw.get("fetch_media")),
        fetch_metabase_db=_parse_fetch_metabase_db(raw.get("fetch_metabase_db")),
        reset_db=_parse_reset_db(raw.get("reset_db")),
        django=_parse_django(raw.get("django")),
        frontend=_parse_frontend(raw.get("frontend")),
        start=_parse_start(raw.get("start")),
        test=_parse_test(raw.get("test")),
    )


def _resolve_native_config(
    data: dict[str, Any],
    *,
    paths: PathsConfig,
    repo_root: Path,
    project_name: str,
) -> NativeConfig:
    """Load ``native:`` from tooling.yaml, with deprecated ``local:`` / ``dev:`` fallbacks."""
    native_raw = data.get("native")
    local_raw = data.get("local")
    dev_raw = data.get("dev")
    if native_raw is not None:
        if local_raw is not None:
            warn_deprecated(
                "local:",
                "native:",
                context="tooling.yaml `local:` is ignored when `native:` is present",
            )
        if dev_raw is not None:
            warn_deprecated(
                "dev:",
                "native:",
                context="tooling.yaml `dev:` is ignored when `native:` is present",
            )
        return _parse_native(
            native_raw,
            paths=paths,
            repo_root=repo_root,
            project_name=project_name,
        )
    if local_raw is not None:
        warn_deprecated("local:", "native:", context="tooling.yaml")
        return _parse_native(
            local_raw,
            paths=paths,
            repo_root=repo_root,
            project_name=project_name,
        )
    if dev_raw is not None:
        warn_deprecated("dev:", "native:", context="tooling.yaml")
        return _parse_native(
            dev_raw,
            paths=paths,
            repo_root=repo_root,
            project_name=project_name,
        )
    return _parse_native(
        None,
        paths=paths,
        repo_root=repo_root,
        project_name=project_name,
    )


def _parse_digitalocean(do_raw: dict[str, Any]) -> DigitalOceanConfig:
    project_name = _optional_str(do_raw, "project_name")
    project_id = _optional_str(do_raw, "project_id")
    if project_name and project_id:
        raise ProjectConfigError(
            "digitalocean: set only one of project_name or project_id"
        )
    ssh_keys_raw = do_raw.get("ssh_keys")
    ssh_keys: tuple[str, ...] | None = None
    if ssh_keys_raw is not None:
        ssh_keys = _parse_string_list(ssh_keys_raw, field="digitalocean.ssh_keys")
        if not ssh_keys:
            ssh_keys = None
    spaces: SpacesConfig | None = None
    spaces_raw = do_raw.get("spaces")
    if spaces_raw is not None:
        if not isinstance(spaces_raw, dict):
            raise ProjectConfigError("digitalocean.spaces must be a mapping")
        spaces = _parse_spaces(spaces_raw)
    return DigitalOceanConfig(
        project_name=project_name,
        project_id=project_id,
        context=_optional_str(do_raw, "context"),
        timezone=_optional_str(do_raw, "timezone"),
        region=_optional_str(do_raw, "region"),
        size=_optional_str(do_raw, "size"),
        image=_optional_str(do_raw, "image"),
        ssh_keys=ssh_keys,
        spaces=spaces,
        monitoring=_optional_bool(do_raw, "monitoring", default=True),
    )


def _parse_pgbr_log_level_field(raw: dict[str, Any], key: str, *, section: str) -> str | None:
    field = f"{section}.{key}"
    try:
        return parse_optional_pgbr_log_level(raw.get(key), field=field)
    except BackupLoggingConfigError as e:
        raise ProjectConfigError(str(e)) from e


def _parse_restic_verbose_field(raw: dict[str, Any], key: str, *, section: str) -> int | None:
    field = f"{section}.{key}"
    try:
        return parse_optional_restic_verbose(raw.get(key), field=field)
    except BackupLoggingConfigError as e:
        raise ProjectConfigError(str(e)) from e


def _parse_ops(
    ops_raw: dict[str, Any],
    *,
    project_name: str,
    repo_root: Path,
    paths: PathsConfig,
) -> OpsConfig:
    pg_raw = ops_raw.get("pgbackrest")
    if pg_raw is None:
        pg_raw = {}
    elif not isinstance(pg_raw, dict):
        raise ProjectConfigError("ops.pgbackrest must be a mapping")

    unit_prefix = _optional_str(ops_raw, "systemd_unit_prefix") or f"{project_name}-"
    default_pg_units, default_restic_units = default_systemd_unit_lists(unit_prefix)

    units_raw = ops_raw.get("systemd_units")
    if units_raw is None:
        pg_units = default_pg_units
        restic_units = default_restic_units
        timers_pg = tuple(u for u in pg_units if u.endswith(".timer"))
        timers_restic = tuple(u for u in restic_units if u.endswith(".timer"))
    else:
        units_map = _require_mapping(units_raw, "ops.systemd_units")
        if "pgbackrest" in units_map:
            pg_units = _parse_string_list(
                units_map.get("pgbackrest"), field="ops.systemd_units.pgbackrest"
            )
        else:
            pg_units = default_pg_units
        if "restic" in units_map:
            restic_units = _parse_string_list(
                units_map.get("restic"), field="ops.systemd_units.restic"
            )
        else:
            restic_units = default_restic_units
        timers_pg = _parse_string_list(
            units_map.get("timers_enable_pgbackrest"),
            field="ops.systemd_units.timers_enable_pgbackrest",
        )
        timers_restic = _parse_string_list(
            units_map.get("timers_enable_restic"),
            field="ops.systemd_units.timers_enable_restic",
        )
        if not timers_pg and len(pg_units) >= 4:
            timers_pg = tuple(u for u in pg_units if u.endswith(".timer"))
        if not timers_restic and restic_units:
            timers_restic = tuple(u for u in restic_units if u.endswith(".timer"))

    systemd_units = SystemdUnitsOpsConfig(
        pgbackrest=pg_units,
        restic=restic_units,
        timers_enable_pgbackrest=timers_pg,
        timers_enable_restic=timers_restic,
    )
    from catalpa_tooling.systemd_render import validate_systemd_units

    unit_errors = validate_systemd_units(systemd_units, unit_prefix)
    if unit_errors:
        raise ProjectConfigError("; ".join(unit_errors))

    restic_raw = ops_raw.get("restic")
    if restic_raw is None:
        restic_raw = {}
    elif not isinstance(restic_raw, dict):
        raise ProjectConfigError("ops.restic must be a mapping")
    restic_data_volume = _validate_compose_volume_key(
        _optional_str(restic_raw, "data_volume") or DEFAULT_RESTIC_DATA_VOLUME,
        field="ops.restic.data_volume",
    )
    restic_backup_path = _optional_str(restic_raw, "backup_path")
    if restic_backup_path is not None and not restic_backup_path.startswith("/"):
        raise ProjectConfigError("ops.restic.backup_path must be an absolute path")
    if restic_backup_path is None:
        restic_backup_path = _storage_volume_host_path_from_info(
            repo_root,
            paths,
            dk_env=DEFAULT_FETCH_MEDIA_DK_ENV,
            volume_key=restic_data_volume,
        )

    zabbix_raw = ops_raw.get("zabbix")
    if zabbix_raw is None:
        zabbix = ZabbixOpsConfig(
            unit_name=f"{unit_prefix}zabbix-agent2.service",
            userparams_file=f"99-{project_name}-userparams.conf",
        )
    else:
        zabbix_map = _require_mapping(zabbix_raw, "ops.zabbix")
        zabbix = ZabbixOpsConfig(
            unit_name=(
                _optional_str(zabbix_map, "unit_name")
                or f"{unit_prefix}zabbix-agent2.service"
            ),
            userparams_file=(
                _optional_str(zabbix_map, "userparams_file")
                or f"99-{project_name}-userparams.conf"
            ),
        )

    transfer = _optional_str(ops_raw, "transfer_workdir") or f".{project_name}-transfer"
    return OpsConfig(
        install_prefix=_optional_str(ops_raw, "install_prefix") or f"/opt/{project_name}",
        config_dir=_optional_str(ops_raw, "config_dir") or f"/etc/{project_name}",
        systemd_unit_prefix=unit_prefix,
        transfer_workdir=_validate_rel_path(transfer, field="ops.transfer_workdir"),
        pgbackrest=PgbackrestOpsConfig(
            postgres_conf=(
                _optional_str(pg_raw, "postgres_conf") or DEFAULT_PGBR_POSTGRES_CONF
            ),
            pgbackrest_conf=(
                _optional_str(pg_raw, "pgbackrest_conf") or DEFAULT_PGBR_CONF
            ),
            default_registry=(
                _optional_str(pg_raw, "default_registry") or DEFAULT_PGBR_REGISTRY
            ),
            restore_temp_prefix=(
                _optional_str(pg_raw, "restore_temp_prefix")
                or default_pgbackrest_restore_temp_prefix(project_name)
            ),
            data_volume=_validate_compose_volume_key(
                _optional_str(pg_raw, "data_volume") or DEFAULT_PGBR_DATA_VOLUME,
                field="ops.pgbackrest.data_volume",
            ),
            pg1_path=_optional_str(pg_raw, "pg1_path") or DEFAULT_PGBR_PG1_PATH,
            log_level_console=_parse_pgbr_log_level_field(
                pg_raw, "log_level_console", section="ops.pgbackrest"
            ),
            log_level_stderr=_parse_pgbr_log_level_field(
                pg_raw, "log_level_stderr", section="ops.pgbackrest"
            ),
            restore_log_level_console=_parse_pgbr_log_level_field(
                pg_raw, "restore_log_level_console", section="ops.pgbackrest"
            ),
        ),
        restic=ResticOpsConfig(
            data_volume=restic_data_volume,
            backup_path=restic_backup_path,
            verbose=_parse_restic_verbose_field(restic_raw, "verbose", section="ops.restic"),
            restore_verbose=_parse_restic_verbose_field(
                restic_raw, "restore_verbose", section="ops.restic"
            ),
        ),
        zabbix=zabbix,
        systemd_units=systemd_units,
        post_db_restore=_parse_post_db_restore(ops_raw.get("post_db_restore")),
        post_metabase_db_restore=_parse_post_metabase_db_restore(
            ops_raw.get("post_metabase_db_restore")
        ),
        default_db_container=(
            _optional_str(ops_raw, "default_db_container") or f"{project_name}-full-db-1"
        ),
    )


def _parse_bundled_assets(raw: Any) -> tuple[ComplianceBundledAssetConfig, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProjectConfigError("compliance.bundled_assets must be a list")
    out: list[ComplianceBundledAssetConfig] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ProjectConfigError(f"compliance.bundled_assets[{idx}] must be a mapping")
        path = _validate_rel_path(
            _require_str(item, "path", section=f"compliance.bundled_assets[{idx}]"),
            field=f"compliance.bundled_assets[{idx}].path",
        )
        globs = _parse_string_list(
            item.get("license_globs"),
            field=f"compliance.bundled_assets[{idx}].license_globs",
        )
        if not globs:
            raise ProjectConfigError(
                f"compliance.bundled_assets[{idx}].license_globs must list at least one glob"
            )
        out.append(ComplianceBundledAssetConfig(path=path, license_globs=globs))
    return tuple(out)


def _parse_compliance_outputs(raw: Any) -> ComplianceOutputsConfig:
    if raw is None or not isinstance(raw, dict):
        raise ProjectConfigError("compliance.outputs must be a mapping")
    return ComplianceOutputsConfig(
        sbom_dir=_validate_rel_path(
            _require_str(raw, "sbom_dir", section="compliance.outputs"),
            field="compliance.outputs.sbom_dir",
        ),
        notices=_validate_rel_path(
            _require_str(raw, "notices", section="compliance.outputs"),
            field="compliance.outputs.notices",
        ),
    )


def _parse_compliance_python(raw: Any) -> CompliancePythonConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProjectConfigError("compliance.python must be a mapping")
    lockfiles = _parse_string_list(raw.get("lockfiles"), field="compliance.python.lockfiles")
    if not lockfiles:
        return None
    validated = tuple(
        _validate_rel_path(rel, field="compliance.python.lockfiles") for rel in lockfiles
    )
    return CompliancePythonConfig(lockfiles=validated)


def _parse_compliance_javascript(raw: Any, *, frontend: str) -> ComplianceJavascriptConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProjectConfigError("compliance.javascript must be a mapping")
    cwd = _optional_str(raw, "cwd")
    if cwd:
        cwd = _validate_rel_path(cwd, field="compliance.javascript.cwd")
    lockfile = _optional_str(raw, "lockfile") or "pnpm-lock.yaml"
    production_only = _optional_bool(raw, "production_only", default=True)
    return ComplianceJavascriptConfig(
        cwd=cwd or frontend,
        lockfile=lockfile,
        production_only=production_only,
    )


def _parse_compliance(raw: Any, *, paths: PathsConfig) -> ComplianceConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProjectConfigError("compliance must be a mapping")
    project_license = _require_str(raw, "project_license", section="compliance")
    if "license_files" not in raw:
        license_files = (f"{paths.frontend}/LICENSE",)
    else:
        license_files = _parse_string_list(
            raw.get("license_files"), field="compliance.license_files"
        )
    license_files = tuple(
        _validate_rel_path(rel, field="compliance.license_files") for rel in license_files
    )
    forbidden = _parse_string_list(raw.get("forbidden_spdx"), field="compliance.forbidden_spdx")
    warn = _parse_string_list(raw.get("warn_spdx"), field="compliance.warn_spdx")
    allowed_packages = _parse_string_list(
        raw.get("allowed_packages"), field="compliance.allowed_packages"
    )
    outputs_raw = raw.get("outputs")
    outputs = (
        _parse_compliance_outputs(outputs_raw)
        if outputs_raw is not None
        else ComplianceOutputsConfig(
            sbom_dir="compliance/sbom",
            notices="compliance/THIRD_PARTY_NOTICES.md",
        )
    )
    javascript_raw = raw.get("javascript")
    javascript = (
        _parse_compliance_javascript(javascript_raw, frontend=paths.frontend)
        if "javascript" in raw
        else None
    )
    return ComplianceConfig(
        project_license=project_license,
        license_files=license_files,
        python=_parse_compliance_python(raw.get("python")),
        javascript=javascript,
        bundled_assets=_parse_bundled_assets(raw.get("bundled_assets")),
        forbidden_spdx=forbidden or DEFAULT_FORBIDDEN_SPDX,
        warn_spdx=warn or DEFAULT_WARN_SPDX,
        allow_strong_copyleft=_optional_bool(raw, "allow_strong_copyleft", default=False),
        outputs=outputs,
        allowed_packages=allowed_packages,
    )


def _infer_compliance_config(
    paths: PathsConfig,
    *,
    lockfile: str,
) -> ComplianceConfig | None:
    """Minimal inferred config when ``compliance:`` is omitted (JS-only)."""
    return ComplianceConfig(
        project_license="UNKNOWN",
        license_files=(),
        python=None,
        javascript=ComplianceJavascriptConfig(
            cwd=paths.frontend,
            lockfile=lockfile,
            production_only=True,
        ),
        bundled_assets=(),
        forbidden_spdx=DEFAULT_FORBIDDEN_SPDX,
        warn_spdx=DEFAULT_WARN_SPDX,
        allow_strong_copyleft=False,
        outputs=ComplianceOutputsConfig(
            sbom_dir="compliance/sbom",
            notices="compliance/THIRD_PARTY_NOTICES.md",
        ),
    )


def resolve_compliance_config(config: ProjectConfig) -> ComplianceConfig | None:
    if config.compliance is not None:
        return config.compliance
    from catalpa_tooling.compliance.javascript_scan import infer_javascript_lockfile

    frontend_dir = config.repo_root / config.paths.frontend
    lockfile = infer_javascript_lockfile(frontend_dir)
    if lockfile is not None:
        return _infer_compliance_config(config.paths, lockfile=lockfile)
    return None


def _parse_manifest(data: dict[str, Any], *, repo_root: Path, tooling_path: Path) -> ProjectConfig:
    project_raw = _require_mapping(data.get("project"), "project")
    paths_raw = _require_mapping(data.get("paths"), "paths")
    # stack/ops are deploy-side and optional: a project can adopt the engine-agnostic
    # commands without declaring compose services, pgbackrest, zabbix, or systemd units.
    stack_raw = data.get("stack")
    ops_raw = data.get("ops")
    meta = ProjectMetaConfig(
        name=_require_str(project_raw, "name", section="project"),
        root_marker=_optional_str(project_raw, "root_marker") or DEFAULT_ROOT_MARKER,
    )
    marker = repo_root / meta.root_marker
    if not marker.is_file():
        raise ProjectConfigError(f"root_marker not found: {marker}")
    digitalocean: DigitalOceanConfig | None = None
    if "digitalocean" in data:
        do_raw = data.get("digitalocean")
        if do_raw is None:
            digitalocean = None
        elif not isinstance(do_raw, dict):
            raise ProjectConfigError("digitalocean must be a mapping")
        else:
            digitalocean = _parse_digitalocean(do_raw)
    dev = _parse_dev(data.get("dev"), digitalocean=digitalocean)
    paths = _parse_paths(paths_raw, project_name=meta.name)
    compliance = _parse_compliance(data.get("compliance"), paths=paths) if "compliance" in data else None
    return ProjectConfig(
        meta=meta,
        paths=paths,
        stack_optional=(
            _parse_stack(_require_mapping(stack_raw, "stack"), project_name=meta.name)
            if stack_raw is not None
            else None
        ),
        ops_optional=(
            _parse_ops(
                _require_mapping(ops_raw, "ops"),
                project_name=meta.name,
                repo_root=repo_root,
                paths=paths,
            )
            if ops_raw is not None
            else None
        ),
        native=_resolve_native_config(
            data,
            paths=paths,
            repo_root=repo_root,
            project_name=meta.name,
        ),
        dev=dev,
        digitalocean=digitalocean,
        compliance=compliance,
        repo_root=repo_root.resolve(),
        tooling_path=tooling_path.resolve(),
    )


def tooling_path_for_repo(repo_root: Path) -> Path:
    env_path = os.environ.get("TOOLING_CONFIG", "").strip()
    if env_path:
        p = Path(env_path).expanduser()
        if not p.is_file():
            raise ProjectConfigError(f"TOOLING_CONFIG file not found: {p}")
        return p.resolve()
    return (repo_root / TOOLING_FILENAME).resolve()


def load_project_config(repo_root: Path) -> ProjectConfig:
    """Load ``tooling.yaml`` from ``repo_root`` (or ``TOOLING_CONFIG`` when set)."""
    repo_root = repo_root.resolve()
    tooling_path = tooling_path_for_repo(repo_root)
    if not tooling_path.is_file():
        raise ProjectConfigError(f"Missing project manifest: {tooling_path}")
    if os.environ.get("TOOLING_CONFIG", "").strip():
        repo_root = tooling_path.parent.resolve()
    try:
        with open(tooling_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ProjectConfigError(f"Invalid YAML in {tooling_path}: {e}") from e
    if not isinstance(data, dict):
        raise ProjectConfigError(f"tooling.yaml root must be a mapping: {tooling_path}")
    return _parse_manifest(data, repo_root=repo_root, tooling_path=tooling_path)
