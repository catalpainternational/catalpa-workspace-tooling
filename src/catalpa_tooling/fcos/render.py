"""``dk <env> render`` / ``dk <env> check``: build, write and compare the generated host files.

A render reads only what anyone with the repository can read: ``info.yaml``, the compose file
and the *names* in ``credentials.yaml`` (SOPS leaves keys in the clear). Each credential is
handed to compose as a marker, never its value, so the render is the same whoever makes it and
holds no secret, and can be committed and compared byte for byte.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.fcos.backups import backup_files, plan_backups, restic_unit_name, restore_spec
from catalpa_tooling.fcos.butane import MASKED_UNITS, host_bu, key_files
from catalpa_tooling.fcos.caddy_json import CaddyJsonError, caddy_json_text, run_adapt
from catalpa_tooling.fcos.monitoring import monitor_dir, monitoring_files
from catalpa_tooling.fcos.quadlets import (
    SECRET_MARK,
    ComposeNotSupported,
    RenderInputs,
    has_secret,
    quadlets_from_compose,
)
from catalpa_tooling.fcos.settings import FcosConfigError, FcosSettings, parse_fcos_settings

#: Under ``docker/envs/<env>/``; ``generated/`` leaves room for renders for other targets.
GENERATED_DIR = "generated/fcos"
MANIFEST = "manifest.json"

Resolver = Callable[[Path, str, dict[str, str]], dict]
#: (image, env, caddyfile, platform) → (adapted config, the image's entrypoint)
Adapter = Callable[[str, dict[str, str], str, "str | None"], tuple[dict, list[str]]]


class RenderError(RuntimeError):
    """The environment cannot be rendered; the message says why."""


@dataclass(frozen=True)
class Render:
    files: dict[str, str]
    """Path under ``docker/envs/<env>/generated/fcos/`` → text."""
    compose_file: str
    env: dict[str, str] = field(default_factory=dict)
    """What compose was resolved with (credentials as markers)."""
    secret_keys: frozenset[str] = frozenset()
    """Credentials the resolved compose file uses."""


def host_manifest_path(config_dir: str) -> str:
    """Where ``apply`` (and ``host.bu``) put the manifest on the host."""
    return f"{config_dir}/fcos-manifest.json"


def generated_dir(config: ProjectConfig, env_name: str) -> Path:
    return config.deploy_envs_dir / env_name / GENERATED_DIR


def credential_keys(path: Path) -> set[str]:
    """The setting names ``credentials.yaml`` gives, read without decrypting."""
    if not path.is_file():
        return set()
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise RenderError(f"{path} is not a mapping")
    return {str(k) for k, v in data.items() if k != "sops" and not isinstance(v, dict | list)}


def resolve_compose(repo_root: Path, compose_file: str, env: dict[str, str]) -> dict:
    """``docker compose config`` with nothing from the operator's shell but PATH and HOME,
    so the render does not depend on who runs it."""
    if not shutil.which("docker"):
        raise RenderError("resolving the compose file needs `docker compose`")
    run_env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    run_env.update(env)
    r = subprocess.run(
        ["docker", "compose", "-f", compose_file, "config", "--format", "json"],
        cwd=repo_root,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if r.returncode != 0:
        raise RenderError(f"docker compose config failed: {r.stderr.strip()[-800:]}")
    return json.loads(r.stdout)


def default_bind_volumes(config: ProjectConfig, settings: FcosSettings) -> tuple[str, ...]:
    """The volumes whose data is worth keeping, from the keys compose deploys already use."""
    if settings.bind_volumes is not None:
        return settings.bind_volumes
    from catalpa_tooling.storage_config import CADDY_DATA_VOLUME_KEY

    keys = [CADDY_DATA_VOLUME_KEY]
    if config.has_ops:
        keys += [config.ops.pgbackrest.data_volume, *config.ops.restic.data_volumes]
    return tuple(sorted(set(keys)))


def previous_ranges(gen_dir: Path) -> dict[str, int]:
    path = gen_dir / MANIFEST
    if not path.is_file():
        return {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: dict[str, int] = {}
    for spec in (manifest.get("id_ranges") or {}).values():
        for svc in spec.get("services") or []:
            out[str(svc)] = int(spec["start"])
    return out


def _read_info(config: ProjectConfig, env_name: str) -> dict:
    path = config.deploy_envs_dir / env_name / "info.yaml"
    if not path.is_file():
        raise RenderError(f"Missing {path}")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def compose_env(
    config: ProjectConfig, env_name: str, info: dict, markers: dict[str, str]
) -> tuple[str, dict[str, str], bool]:
    """(compose file, settings compose resolves with, whether images are pulled by tag), as
    ``dk <env>`` builds them, with credentials as markers and no SSH or SOPS."""
    from catalpa_tooling.managed_deploy_env import load_managed_deploy_context

    ctx = load_managed_deploy_context(
        config,
        env_name,
        info=info,
        apply_worktree=False,
        credentials=markers,
        register_host_key=False,
    )
    if ctx is None:
        raise RenderError(f"could not build {env_name}'s settings (the message is above)")
    env = {k: v for k, v in ctx.env_add.items() if k != "DOCKER_HOST"}
    if not (env.get("COMPOSE_PROJECT_NAME") or "").strip():
        # The project name a compose deploy to a remote host gets (resolve_env_with_compose_project),
        # so a root `name: ${PROJECT_NAME}` that a shell (direnv) fills doesn't stop the render.
        from catalpa_tooling.restic_files import _default_compose_project

        env["COMPOSE_PROJECT_NAME"] = _default_compose_project(config)
    # Build-time stamps for SPA bundles: they change on every call and mean nothing at runtime.
    for key in ("VITE_BUILD_TIME", "VITE_GIT_SHA"):
        if key in env:
            env[key] = ""
    return ctx.compose_file, env, ctx.use_prepulled_registry


def render_env(
    config: ProjectConfig,
    env_name: str,
    *,
    resolver: Resolver = resolve_compose,
    adapter: Adapter = run_adapt,
) -> Render:
    info = _read_info(config, env_name)
    try:
        settings = parse_fcos_settings(info)
    except FcosConfigError as exc:
        raise RenderError(str(exc)) from exc
    from catalpa_tooling.ssh_known_hosts import ssh_host_from_docker_host

    if not ssh_host_from_docker_host(str(info.get("docker_host") or "")):
        # Without it the env's settings are a local one's (http:// site addresses, localhost).
        raise RenderError(
            f"{env_name}: a rendered host is remote, so info.yaml needs an ssh:// docker_host "
            f"(dk reaches the host through it, and the settings follow it), e.g. "
            f"docker_host: ssh://deploy@<host>"
        )

    from catalpa_tooling.project_credentials import (
        env_credentials_path,
        project_credentials_path,
    )

    # The project's credentials, and the env's (which override a project value of the same name).
    keys = credential_keys(project_credentials_path(config)) | credential_keys(
        env_credentials_path(config, env_name)
    )
    markers = {k: SECRET_MARK.format(key=k.upper()) for k in keys}
    compose_file, env, pulled = compose_env(config, env_name, info, markers)
    if not pulled:
        raise RenderError(
            f"{env_name}: the host pulls its images, so info.yaml needs image_tag (and a registry)"
        )
    resolved = resolver(config.repo_root, compose_file, env)
    text = json.dumps(resolved)
    used = frozenset(k.upper() for k in keys if SECRET_MARK.format(key=k.upper()) in text)

    gen_dir = generated_dir(config, env_name)
    project = config.meta.name
    bind_volumes = default_bind_volumes(config, settings)
    if settings.bind_volumes is None:
        # Derived from ops: keep the ones this compose file has (an explicit list must match).
        declared = set(resolved.get("volumes") or {})
        bind_volumes = tuple(v for v in bind_volumes if v in declared)
    config_dir = config.ops.config_dir if config.has_ops else f"/etc/{project}"
    compose_rel = os.path.relpath(compose_file, config.repo_root)
    hostname = settings.hostname or _default_hostname(project, env_name)
    db_service = config.stack.services.db
    from catalpa_tooling.backup_settings import BackupSettingsError, backup_settings

    try:
        roles = backup_settings(config, env_name, info)
    except BackupSettingsError as exc:
        raise RenderError(str(exc)) from exc
    source = roles.restore_from
    plan = plan_backups(
        config,
        settings,
        keys,
        project=project,
        declared_volumes=set(resolved.get("volumes") or {}),
        roles=roles,
    )
    if plan.problems:
        raise RenderError("backups:\n" + "\n".join(f"  - {p}" for p in plan.problems))
    restic_container = (restic_unit_name(config) or "") if plan.restic else ""
    inputs = RenderInputs(
        env_name=env_name,
        project=project,
        compose_rel=compose_rel,
        proxy=config.stack.services.proxy,
        web=config.stack.services.web or "",
        settings=settings,
        bind_volumes=bind_volumes,
        config_dir=config_dir,
        previous_ranges=previous_ranges(gen_dir),
        extra_lines={db_service: plan.db_lines} if plan.db_lines else {},
        extra_volume_users={v: (restic_container,) for v in plan.restic_volumes} if restic_container else {},
        volume_owners=_volume_owners(config, settings),
    )
    try:
        q = quadlets_from_compose(resolved, inputs)
        adapted, image_entrypoint = adapter(
            q.proxy_image, q.proxy_env, settings.caddyfile, q.proxy_platform
        )
        proxy_svc = (resolved.get("services") or {}).get(inputs.proxy) or {}
        if proxy_svc.get("entrypoint") or image_entrypoint:
            q = quadlets_from_compose(resolved, replace(inputs, proxy_wrapped=True))
        caddy_json = caddy_json_text(adapted)
    except (ComposeNotSupported, CaddyJsonError) as exc:
        raise RenderError(str(exc)) from exc
    if has_secret(caddy_json):
        raise RenderError(
            "the edge's Caddyfile uses a credential at adapt time ({$VAR}); caddy.json is "
            "committed, so read it at runtime instead ({env.VAR})"
        )
    files = dict(q.files)
    files["caddy/caddy.json"] = caddy_json
    manifest = dict(q.manifest)

    from catalpa_tooling.monitoring_setting import MonitoringSettingError, monitoring_of

    try:
        monitoring = monitoring_of(info)
    except MonitoringSettingError as exc:
        raise RenderError(f"{env_name}: {exc}") from exc
    monitoring_on = monitoring == "zabbix"
    if monitoring_on and not (config.has_ops and config.ops.zabbix is not None):
        raise RenderError(
            f"{env_name}: monitoring: zabbix, but tooling.yaml has no ops.zabbix (the agent's unit "
            "and userparams file names)"
        )
    backups_on = bool(plan.pgbackrest or plan.restic)
    mon_dir = monitor_dir(project) if (monitoring_on and backups_on) else None
    try:
        b = backup_files(
            plan,
            config,
            settings,
            header=q.header,
            project=project,
            compose_project=q.compose_project,
            hostname=hostname,
            db_service=db_service,
            bind_volumes=bind_volumes,
            service_ranges=q.service_ranges,
            writers=q.volume_writers,
            monitor_dir=mon_dir,
            env=env,
        )
        restore = restore_spec(
            plan, config, settings, source=source, bind_volumes=bind_volumes,
            service_ranges=q.service_ranges, writers=q.volume_writers,
        )  # fmt: skip
    except ValueError as exc:
        raise RenderError(f"backups: {exc}") from exc
    files.update(b.files)
    install = list(q.install) + b.install
    timers = list(b.timers)
    system_secrets: dict[str, str] = {}
    if monitoring_on:
        from catalpa_tooling.remote_deploy import _zabbix_env_defaults
        from catalpa_tooling.zabbix_systemd import hostname_from_site_origin

        zbx_env = _zabbix_env_defaults(info, env)
        if not (zbx_env.get("ZBX_TLSPSK") and zbx_env.get("ZBX_TLSPSKIDENTITY")):
            print(
                f"warning: {env_name}: monitoring: zabbix without zbx_tlspsk / zbx_tlspskidentity "
                "in credentials: the agent talks to the server unencrypted",
                file=sys.stderr,
            )
        site_host = hostname_from_site_origin(env.get("SITE_ORIGIN"))
        try:
            m = monitoring_files(
                settings=settings,
                header=q.header,
                project=project,
                config_dir=config_dir,
                userparams_file=config.ops.zabbix.userparams_file,
                zbx_env=zbx_env,
                hostname=site_host or hostname,
                backups=tuple(k for k in ("pgbackrest", "restic") if getattr(plan, k)),
            )
        except ValueError as exc:
            raise RenderError(f"zabbix: {exc}") from exc
        files.update(m.files)
        install += m.install
        system_secrets = m.system_secrets

    # After monitoring, so its proxy's image is pulled first too.
    from catalpa_tooling.fcos.auto_update import auto_update_files

    au_files, au_install = auto_update_files(settings, q.header, files, install)
    files.update(au_files)
    install += au_install

    try:
        key_text, key_install = key_files(settings, q.header)
    except ValueError as exc:
        raise RenderError(str(exc)) from exc
    files.update(key_text)
    install += key_install
    # The manifest goes on the host too: `dk <env> diff`/`apply` read the applied one back to find
    # files a later render no longer has.
    install.append(
        {"file": MANIFEST, "dest": host_manifest_path(config_dir), "owner": "root", "mode": "0644"}
    )
    install = sorted(install, key=lambda i: i["file"])
    timezone = settings.timezone or (
        (config.digitalocean.timezone if config.digitalocean else None) or "UTC"
    )
    try:
        files["host.bu"] = host_bu(
            settings=settings,
            header=q.header,
            hostname=hostname,
            timezone=timezone,
            install=install,
            bind_volumes=manifest["bind_volumes"],
            proxy=inputs.proxy,
            timers=timers,
            monitor_dir=mon_dir,
            config_dir=config_dir,
        )
    except ValueError as exc:
        raise RenderError(str(exc)) from exc

    manifest.update(
        {
            "hostname": hostname,
            "timezone": timezone,
            "config_dir": config_dir,
            "masked_units": list(MASKED_UNITS),
            "proxy": inputs.proxy,
            "secrets": dict(sorted({**manifest["secrets"], **plan.secrets}.items())),
            "system_secrets": dict(sorted(system_secrets.items())),
            "timers": sorted(timers),
            "install": install,
        }
    )
    if restore:
        manifest["restore"] = restore
    files[MANIFEST] = json.dumps(manifest, indent=2) + "\n"
    return Render(files=files, compose_file=compose_file, env=env, secret_keys=used)


def _default_hostname(project: str, env_name: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", f"{project}-{env_name}".lower()).strip("-")
    return name[:63].rstrip("-")


def _volume_owners(config: ProjectConfig, settings: FcosSettings) -> dict[str, int]:
    """The uid inside its range that owns each bind volume: postgres (999 in the Debian-based
    images) for the pgBackRest data volume, root otherwise, unless fcos.volume_owners says."""
    owners: dict[str, int] = {}
    if config.has_ops:
        owners[config.ops.pgbackrest.data_volume] = 999
    owners.update(settings.volume_owners)
    return owners


def required_secret_problems(
    config: ProjectConfig, render: Render, *, resolver: Resolver = resolve_compose
) -> list[str]:
    """Each credential the compose file uses must make plain ``docker compose`` refuse to start
    without it (``${KEY:?…}``), never fall back to a default or to empty."""
    problems = []
    for key in sorted(render.secret_keys):
        env = {k: v for k, v in render.env.items() if k != key}
        try:
            resolver(config.repo_root, render.compose_file, env)
        except RenderError:
            continue
        problems.append(
            f"{key}: compose resolves without it; write it as ${{{key}:?{key} must be set}} "
            "so plain `docker compose up` refuses to start without the secret"
        )
    return problems


def write_render(gen_dir: Path, files: dict[str, str]) -> list[str]:
    """Write ``files`` under ``gen_dir``, removing files a previous render left. Returns the
    paths that changed."""
    changed = compare_render(gen_dir, files)
    if gen_dir.exists():
        for path in sorted(gen_dir.rglob("*"), reverse=True):
            rel = path.relative_to(gen_dir).as_posix()
            if path.is_file() and rel not in files:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    for rel, text in files.items():
        target = gen_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
    return changed


def compare_render(gen_dir: Path, files: dict[str, str]) -> list[str]:
    """Differences between the committed render and ``files``, as ``+ path`` (new),
    ``- path`` (gone) and ``~ path`` (changed)."""
    committed = (
        {
            p.relative_to(gen_dir).as_posix(): p
            for p in gen_dir.rglob("*")
            if p.is_file()
        }
        if gen_dir.is_dir()
        else {}
    )
    out = [f"+ {rel}" for rel in sorted(set(files) - set(committed))]
    out += [f"- {rel}" for rel in sorted(set(committed) - set(files))]
    out += [
        f"~ {rel}"
        for rel in sorted(set(files) & set(committed))
        if committed[rel].read_text(encoding="utf-8") != files[rel]
    ]
    return sorted(out, key=lambda line: line[2:])
