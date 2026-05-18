"""Load and validate repo-root ``tooling.yaml`` (project manifest)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.repo_paths import TOOLING_FILENAME, repo_root_from_cwd

DEFAULT_ROOT_MARKER = "pyproject.toml"


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


def _optional_str(data: dict[str, Any], key: str) -> str | None:
    if key not in data or data[key] is None or data[key] is False:
        return None
    s = str(data[key]).strip()
    return s or None


def _validate_rel_path(rel: str, *, field: str) -> str:
    if not rel or ".." in Path(rel).parts:
        raise ProjectConfigError(f"Invalid relative path for {field}: {rel!r}")
    return rel


@dataclass(frozen=True)
class DeployPathsConfig:
    envs_dir: str
    images_config: str
    default_compose: str
    dev_compose: str
    credentials_optional_envs: tuple[str, ...]


@dataclass(frozen=True)
class PathsConfig:
    backend: str
    frontend: str
    prototype: str | None
    scripts: str
    env_local: str
    email_backend_dir: str
    fetch_db_dump: str
    deploy: DeployPathsConfig


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


@dataclass(frozen=True)
class PgbackrestOpsConfig:
    postgres_conf: str
    pgbackrest_conf: str
    default_registry: str
    restore_temp_prefix: str


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
class OpsConfig:
    install_prefix: str
    config_dir: str
    systemd_unit_prefix: str
    transfer_workdir: str
    pgbackrest: PgbackrestOpsConfig
    zabbix: ZabbixOpsConfig
    systemd_units: SystemdUnitsOpsConfig
    default_db_container: str


@dataclass(frozen=True)
class ProjectMetaConfig:
    name: str
    root_marker: str


@dataclass(frozen=True)
class ProjectConfig:
    """Typed view of ``tooling.yaml`` with resolved paths under ``repo_root``."""

    meta: ProjectMetaConfig
    paths: PathsConfig
    stack: StackConfig
    ops: OpsConfig
    repo_root: Path
    tooling_path: Path

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
        return self.repo_root / self.paths.scripts

    @property
    def env_local_path(self) -> Path:
        return self.repo_root / self.paths.env_local

    @property
    def email_backend_dir(self) -> Path:
        return self.repo_root / self.paths.email_backend_dir

    @property
    def fetch_db_dump_path(self) -> Path:
        return self.repo_root / self.paths.fetch_db_dump

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
        if env_name in self.paths.deploy.credentials_optional_envs:
            return True
        return env_name.startswith("local_")

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


def _parse_paths(paths_raw: dict[str, Any]) -> PathsConfig:
    deploy_raw = _require_mapping(paths_raw.get("deploy"), "paths.deploy")
    deploy = DeployPathsConfig(
        envs_dir=_validate_rel_path(
            _require_str(deploy_raw, "envs_dir", section="paths.deploy"), field="paths.deploy.envs_dir"
        ),
        images_config=_validate_rel_path(
            _require_str(deploy_raw, "images_config", section="paths.deploy"),
            field="paths.deploy.images_config",
        ),
        default_compose=_validate_rel_path(
            _require_str(deploy_raw, "default_compose", section="paths.deploy"),
            field="paths.deploy.default_compose",
        ),
        dev_compose=_validate_rel_path(
            _require_str(deploy_raw, "dev_compose", section="paths.deploy"),
            field="paths.deploy.dev_compose",
        ),
        credentials_optional_envs=_parse_string_list(
            deploy_raw.get("credentials_optional_envs"),
            field="paths.deploy.credentials_optional_envs",
        ),
    )
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
        scripts=_validate_rel_path(
            _require_str(paths_raw, "scripts", section="paths"), field="paths.scripts"
        ),
        env_local=_validate_rel_path(
            _require_str(paths_raw, "env_local", section="paths"), field="paths.env_local"
        ),
        email_backend_dir=_validate_rel_path(
            _require_str(paths_raw, "email_backend_dir", section="paths"),
            field="paths.email_backend_dir",
        ),
        fetch_db_dump=_validate_rel_path(
            _require_str(paths_raw, "fetch_db_dump", section="paths"), field="paths.fetch_db_dump"
        ),
        deploy=deploy,
    )


def _parse_stack(stack_raw: dict[str, Any]) -> StackConfig:
    services_raw = _require_mapping(stack_raw.get("services"), "stack.services")
    images_raw = _require_mapping(stack_raw.get("images"), "stack.images")
    components_raw = _require_mapping(images_raw.get("components"), "stack.images.components")
    health_raw = _require_mapping(stack_raw.get("healthcheck"), "stack.healthcheck")
    components: dict[str, str] = {}
    for key, val in components_raw.items():
        components[str(key)] = _require_str({str(key): val}, str(key), section="stack.images.components")
    return StackConfig(
        compose_project_default=_require_str(stack_raw, "compose_project_default", section="stack"),
        services=StackServicesConfig(
            web=_require_str(services_raw, "web", section="stack.services"),
            proxy=_require_str(services_raw, "proxy", section="stack.services"),
            db=_require_str(services_raw, "db", section="stack.services"),
        ),
        images=StackImagesConfig(
            registry_key=_require_str(images_raw, "registry_key", section="stack.images"),
            components=components,
        ),
        healthcheck=StackHealthcheckConfig(
            service=_require_str(health_raw, "service", section="stack.healthcheck"),
            url=_require_str(health_raw, "url", section="stack.healthcheck"),
        ),
    )


def _parse_ops(ops_raw: dict[str, Any]) -> OpsConfig:
    pg_raw = _require_mapping(ops_raw.get("pgbackrest"), "ops.pgbackrest")
    zabbix_raw = _require_mapping(ops_raw.get("zabbix"), "ops.zabbix")
    units_raw = _require_mapping(ops_raw.get("systemd_units"), "ops.systemd_units")
    pg_units = _parse_string_list(units_raw.get("pgbackrest"), field="ops.systemd_units.pgbackrest")
    restic_units = _parse_string_list(units_raw.get("restic"), field="ops.systemd_units.restic")
    timers_pg = _parse_string_list(
        units_raw.get("timers_enable_pgbackrest"),
        field="ops.systemd_units.timers_enable_pgbackrest",
    )
    timers_restic = _parse_string_list(
        units_raw.get("timers_enable_restic"),
        field="ops.systemd_units.timers_enable_restic",
    )
    if not timers_pg and len(pg_units) >= 4:
        timers_pg = tuple(u for u in pg_units if u.endswith(".timer"))
    if not timers_restic and restic_units:
        timers_restic = tuple(u for u in restic_units if u.endswith(".timer"))
    return OpsConfig(
        install_prefix=_require_str(ops_raw, "install_prefix", section="ops"),
        config_dir=_require_str(ops_raw, "config_dir", section="ops"),
        systemd_unit_prefix=_require_str(ops_raw, "systemd_unit_prefix", section="ops"),
        transfer_workdir=_validate_rel_path(
            _require_str(ops_raw, "transfer_workdir", section="ops"), field="ops.transfer_workdir"
        ),
        pgbackrest=PgbackrestOpsConfig(
            postgres_conf=_require_str(pg_raw, "postgres_conf", section="ops.pgbackrest"),
            pgbackrest_conf=_require_str(pg_raw, "pgbackrest_conf", section="ops.pgbackrest"),
            default_registry=_require_str(pg_raw, "default_registry", section="ops.pgbackrest"),
            restore_temp_prefix=_require_str(pg_raw, "restore_temp_prefix", section="ops.pgbackrest"),
        ),
        zabbix=ZabbixOpsConfig(
            unit_name=_require_str(zabbix_raw, "unit_name", section="ops.zabbix"),
            userparams_file=_require_str(zabbix_raw, "userparams_file", section="ops.zabbix"),
        ),
        systemd_units=SystemdUnitsOpsConfig(
            pgbackrest=pg_units,
            restic=restic_units,
            timers_enable_pgbackrest=timers_pg,
            timers_enable_restic=timers_restic,
        ),
        default_db_container=_require_str(ops_raw, "default_db_container", section="ops"),
    )


def _parse_manifest(data: dict[str, Any], *, repo_root: Path, tooling_path: Path) -> ProjectConfig:
    project_raw = _require_mapping(data.get("project"), "project")
    paths_raw = _require_mapping(data.get("paths"), "paths")
    stack_raw = _require_mapping(data.get("stack"), "stack")
    ops_raw = _require_mapping(data.get("ops"), "ops")
    meta = ProjectMetaConfig(
        name=_require_str(project_raw, "name", section="project"),
        root_marker=_optional_str(project_raw, "root_marker") or DEFAULT_ROOT_MARKER,
    )
    marker = repo_root / meta.root_marker
    if not marker.is_file():
        raise ProjectConfigError(f"root_marker not found: {marker}")
    return ProjectConfig(
        meta=meta,
        paths=_parse_paths(paths_raw),
        stack=_parse_stack(stack_raw),
        ops=_parse_ops(ops_raw),
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
