"""Garage + Caddy stack deploy on ``dc_backup_docker_host``."""

from __future__ import annotations

import importlib.resources
import secrets
import sys
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dc_backup.paths import (
    DC_BACKUP_FILENAME,
    DEFAULT_CADDY_IMAGE,
    DEFAULT_GARAGE_IMAGE,
    DEFAULT_GARAGE_REGION,
    GARAGE_CADDY_DATA_DIR,
    GARAGE_CADDYFILE_PATH,
    GARAGE_COMPOSE_DIR,
    GARAGE_COMPOSE_FILE,
    GARAGE_DATA_DIR,
    GARAGE_META_DIR,
    GARAGE_TLS_CA_NAME,
    GARAGE_TLS_DIR,
    GARAGE_TLS_SERVER_CRT_NAME,
    GARAGE_TLS_SERVER_KEY_NAME,
    GARAGE_TOML_PATH,
    INFO_DC_BACKUP_DOCKER_HOST,
)
from catalpa_tooling.dc_backup.ssh_install import (
    install_files_via_ssh,
    remote_path_exists,
    remote_run,
)
from catalpa_tooling.sops_credentials import (
    SopsCommandError,
    SopsNotFoundError,
    decrypt_sops_yaml,
    ensure_sops_available,
    write_encrypted_yaml,
)
from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

KEY_RPC = "garage_rpc_secret"
KEY_ADMIN = "garage_admin_token"
KEY_REGION = "garage_s3_region"
KEY_GARAGE_IMAGE = "garage_image"
KEY_CADDY_IMAGE = "caddy_image"


def dc_backup_path(config: ProjectConfig, env_name: str) -> Path:
    return config.deploy_envs_dir / env_name / DC_BACKUP_FILENAME


def _read_info(config: ProjectConfig, env_name: str) -> dict[str, Any]:
    info_path = config.deploy_envs_dir / env_name / "info.yaml"
    if not info_path.is_file():
        raise FileNotFoundError(f"Missing {info_path}")
    with open(info_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _template_text(name: str) -> str:
    root = importlib.resources.files("catalpa_tooling.dc_backup")
    return (root / "templates" / name).read_text(encoding="utf-8")


def render_garage_toml(*, rpc_secret: str, admin_token: str, region: str) -> str:
    text = _template_text("garage.toml.tmpl")
    return (
        text.replace("@RPC_SECRET@", rpc_secret)
        .replace("@ADMIN_TOKEN@", admin_token)
        .replace("@S3_REGION@", region)
    )


def cmd_dc_backup_bootstrap(
    config: ProjectConfig,
    env_name: str,
    *,
    force: bool,
    dry_run: bool,
) -> int:
    path = dc_backup_path(config, env_name)
    if path.is_file() and not force:
        print(
            f"{path} already exists; refuse to overwrite without --force.",
            file=sys.stderr,
        )
        return 1

    data = {
        KEY_RPC: secrets.token_hex(32),
        KEY_ADMIN: secrets.token_urlsafe(32),
        KEY_REGION: DEFAULT_GARAGE_REGION,
        KEY_GARAGE_IMAGE: DEFAULT_GARAGE_IMAGE,
        KEY_CADDY_IMAGE: DEFAULT_CADDY_IMAGE,
    }

    if dry_run:
        print(
            f"dry-run: would write SOPS {path} "
            f"(keys: {KEY_RPC}, {KEY_ADMIN}, {KEY_REGION}, images).",
            flush=True,
        )
        return 0

    try:
        ensure_sops_available()
        write_encrypted_yaml(path, data)
    except (SopsNotFoundError, SopsCommandError) as e:
        print(str(e), file=sys.stderr)
        return 1

    print(
        f"Wrote SOPS {path} (rpc/admin secrets generated; not printed). "
        f"Next: `dk {env_name} dc-backup tls install` if needed, then "
        f"`dk {env_name} dc-backup install --up`.",
        flush=True,
    )
    return 0


def _require_garage_tls_on_host(ssh: str, *, dry_run: bool) -> int:
    if dry_run:
        print(
            f"[dry-run] would require {GARAGE_TLS_DIR}/"
            f"{{{GARAGE_TLS_CA_NAME},{GARAGE_TLS_SERVER_CRT_NAME},{GARAGE_TLS_SERVER_KEY_NAME}}}",
            flush=True,
        )
        return 0
    missing: list[str] = []
    for name in (GARAGE_TLS_CA_NAME, GARAGE_TLS_SERVER_CRT_NAME, GARAGE_TLS_SERVER_KEY_NAME):
        remote = f"{GARAGE_TLS_DIR}/{name}"
        st = remote_path_exists(ssh, remote)
        if st is not True:
            missing.append(remote)
    if missing:
        print(
            "Missing TLS files on DC backup host (run `dk <env> dc-backup tls install` first):\n  "
            + "\n  ".join(missing),
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_dc_backup_install(
    config: ProjectConfig,
    env_name: str,
    *,
    up: bool,
    dry_run: bool,
) -> int:
    info = _read_info(config, env_name)
    backup_host = str(info.get(INFO_DC_BACKUP_DOCKER_HOST, "") or "").strip()
    if not backup_host:
        print(
            f"{INFO_DC_BACKUP_DOCKER_HOST} is unset in docker/envs/{env_name}/info.yaml.",
            file=sys.stderr,
        )
        return 1

    sops_path = dc_backup_path(config, env_name)
    if not sops_path.is_file():
        print(
            f"Missing {sops_path}. Run `dk {env_name} dc-backup bootstrap` first.",
            file=sys.stderr,
        )
        return 1

    try:
        ssh = parse_docker_host_to_ssh_target(backup_host)
        data = decrypt_sops_yaml(sops_path)
    except (ValueError, SopsNotFoundError, SopsCommandError) as e:
        print(str(e), file=sys.stderr)
        return 1

    rpc = str(data.get(KEY_RPC) or "").strip()
    admin = str(data.get(KEY_ADMIN) or "").strip()
    region = str(data.get(KEY_REGION) or DEFAULT_GARAGE_REGION).strip() or DEFAULT_GARAGE_REGION
    garage_image = str(data.get(KEY_GARAGE_IMAGE) or DEFAULT_GARAGE_IMAGE).strip()
    caddy_image = str(data.get(KEY_CADDY_IMAGE) or DEFAULT_CADDY_IMAGE).strip()
    if not rpc or not admin:
        print(
            f"{sops_path} missing {KEY_RPC} / {KEY_ADMIN}; re-run bootstrap --force.",
            file=sys.stderr,
        )
        return 1

    rc = _require_garage_tls_on_host(ssh, dry_run=dry_run)
    if rc != 0:
        return rc

    # Create dirs; ensure toml/Caddyfile are files (not Docker-created directories).
    prep = (
        f"sudo mkdir -p {GARAGE_COMPOSE_DIR} {GARAGE_TLS_DIR} "
        f"{GARAGE_META_DIR} {GARAGE_DATA_DIR} {GARAGE_CADDY_DATA_DIR} /etc/garage && "
        f"sudo chmod 700 {GARAGE_META_DIR} {GARAGE_DATA_DIR} && "
        f"for p in {GARAGE_TOML_PATH} {GARAGE_CADDYFILE_PATH}; do "
        f"  if [ -d \"$p\" ]; then echo \"ERROR: $p is a directory (Docker bind trap)\"; exit 1; fi; "
        f"done"
    )
    rc = remote_run(ssh, prep, dry_run=dry_run)
    if rc != 0:
        return rc

    compose = _template_text("docker-compose.yml")
    # Pin images from SOPS into the written compose (env substitution still works if edited).
    compose = compose.replace("${GARAGE_IMAGE:-dxflrs/garage:v2.3.0}", garage_image)
    compose = compose.replace("${CADDY_IMAGE:-caddy:2.9-alpine}", caddy_image)

    toml = render_garage_toml(rpc_secret=rpc, admin_token=admin, region=region)
    caddyfile = _template_text("Caddyfile")

    # Write compose into /opt/garage; toml + Caddyfile as files under /etc.
    print(f"Installing Garage stack files on {ssh} …", flush=True)
    rc = install_files_via_ssh(
        ssh,
        GARAGE_COMPOSE_DIR,
        [("docker-compose.yml", compose, 0o644)],
        dry_run=dry_run,
    )
    if rc != 0:
        return rc

    # garage.toml is /etc/garage.toml (file next to /etc/garage/ dir)
    rc = install_files_via_ssh(
        ssh,
        "/etc",
        [("garage.toml", toml, 0o600)],
        dry_run=dry_run,
    )
    if rc != 0:
        return rc

    rc = install_files_via_ssh(
        ssh,
        "/etc/garage",
        [("Caddyfile", caddyfile, 0o644)],
        dry_run=dry_run,
    )
    if rc != 0:
        return rc

    if up:
        print(f"Starting stack: docker compose -f {GARAGE_COMPOSE_FILE} up -d", flush=True)
        rc = remote_run(
            ssh,
            f"cd {GARAGE_COMPOSE_DIR} && sudo docker compose -f {GARAGE_COMPOSE_FILE} up -d",
            dry_run=dry_run,
        )
        if rc != 0:
            return rc
    else:
        print(
            f"Files installed. Start with: "
            f"ssh {ssh} 'cd {GARAGE_COMPOSE_DIR} && docker compose up -d' "
            f"(or re-run with --up).",
            flush=True,
        )

    print(
        f"After first start, run `dk {env_name} dc-backup provision` "
        f"to create the Garage bucket/key and WRITE credentials "
        f"(not done automatically by `dk {env_name} db` / `files`).",
        flush=True,
    )
    return 0


def cmd_dc_backup_status(
    config: ProjectConfig,
    env_name: str,
    *,
    check_remote: bool,
) -> int:
    path = dc_backup_path(config, env_name)
    print(f"SOPS file: {path}", flush=True)
    if not path.is_file():
        print("  exists: no", flush=True)
        tls_ok = False
    else:
        print("  exists: yes", flush=True)
        try:
            data = decrypt_sops_yaml(path)
            for key in (KEY_RPC, KEY_ADMIN, KEY_REGION, KEY_GARAGE_IMAGE):
                present = bool(str(data.get(key) or "").strip())
                print(f"  {key}: {'present' if present else 'missing'}", flush=True)
        except (SopsNotFoundError, SopsCommandError) as e:
            print(f"  decrypt: failed ({e})", file=sys.stderr)
            return 1
        tls_ok = True

    info = _read_info(config, env_name)
    backup_host = str(info.get(INFO_DC_BACKUP_DOCKER_HOST, "") or "").strip()
    print(f"  {INFO_DC_BACKUP_DOCKER_HOST}: {backup_host or '(unset)'}", flush=True)

    if path.is_file() and backup_host:
        print(
            f"Hint: bucket/key + WRITE credentials → `dk {env_name} dc-backup provision` "
            f"(then recreate `db` and run backups; db/files do not auto-provision Garage).",
            flush=True,
        )

    if not check_remote or not backup_host:
        return 0 if tls_ok or path.is_file() else 1

    try:
        ssh = parse_docker_host_to_ssh_target(backup_host)
    except ValueError as e:
        print(f"  remote: invalid host ({e})", flush=True)
        return 1

    for remote in (
        GARAGE_COMPOSE_FILE,
        GARAGE_TOML_PATH,
        GARAGE_CADDYFILE_PATH,
        f"{GARAGE_TLS_DIR}/{GARAGE_TLS_SERVER_CRT_NAME}",
        f"{GARAGE_TLS_DIR}/{GARAGE_TLS_SERVER_KEY_NAME}",
    ):
        st = remote_path_exists(ssh, remote)
        if st is True:
            print(f"  remote {remote}: present", flush=True)
        elif st is False:
            print(f"  remote {remote}: missing", flush=True)
        else:
            print(f"  remote {remote}: unreachable", flush=True)
    return 0
