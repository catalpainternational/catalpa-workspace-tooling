"""Daily OOH rclone copy: Garage (backup host) → external S3."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.cli_confirm import confirm_by_typing_env_name
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dc_backup.paths import INFO_DC_BACKUP_DOCKER_HOST
from catalpa_tooling.dc_backup.ssh_install import install_files_via_ssh, remote_run, remote_run_capture
from catalpa_tooling.env_yaml import _credentials_to_env
from catalpa_tooling.sops_credentials import (
    SopsCommandError,
    SopsNotFoundError,
    decrypt_credentials_yaml,
    ensure_sops_available,
)
from catalpa_tooling.ssh_known_hosts import ensure_ssh_known_host_for_docker_host
from catalpa_tooling.systemd_assets import systemd_source_dir
from catalpa_tooling.systemd_remote_install import (
    parse_docker_host_to_ssh_target,
    redact_env_file_content,
)
from catalpa_tooling.systemd_render import render_systemd_unit

REMOTE_SYSTEMD = "/etc/systemd/system"
ENV_FILENAME = "rclone-garage-offsite.env"
SCRIPT_FILENAME = "rclone-garage-offsite.sh"
UNIT_SUFFIX_SERVICE = "rclone-garage-offsite.service"
UNIT_SUFFIX_TIMER = "rclone-garage-offsite.timer"
GARAGE_LOOPBACK_ENDPOINT = "http://127.0.0.1:3900"


def offsite_unit_names(config: ProjectConfig) -> tuple[str, str]:
    """Return ``(service, timer)`` unit filenames from ``ops.systemd_unit_prefix``."""
    prefix = (config.ops.systemd_unit_prefix or "").strip()
    return f"{prefix}{UNIT_SUFFIX_SERVICE}", f"{prefix}{UNIT_SUFFIX_TIMER}"


def validate_offsite_env(env: dict[str, str]) -> str | None:
    """Return an error if Garage WRITE or offsite dest credentials are incomplete."""
    garage_need = (
        ("PGBR_S3_WRITE_BUCKET", env.get("PGBR_S3_WRITE_BUCKET")),
        ("PGBR_S3_WRITE_KEY", env.get("PGBR_S3_WRITE_KEY")),
        ("PGBR_S3_WRITE_SECRET", env.get("PGBR_S3_WRITE_SECRET")),
    )
    missing_g = [k for k, v in garage_need if not (v or "").strip()]
    if missing_g:
        return (
            "Garage source credentials incomplete (need pgbr_s3_write_bucket/key/secret "
            f"from dc-backup provision): missing {', '.join(missing_g)}"
        )

    offsite_need = (
        ("OFFSITE_S3_BUCKET", env.get("OFFSITE_S3_BUCKET")),
        ("OFFSITE_S3_ACCESS_KEY_ID", env.get("OFFSITE_S3_ACCESS_KEY_ID")),
        ("OFFSITE_S3_SECRET_ACCESS_KEY", env.get("OFFSITE_S3_SECRET_ACCESS_KEY")),
    )
    missing_o = [k for k, v in offsite_need if not (v or "").strip()]
    if missing_o:
        return (
            "Offsite destination incomplete; set offsite_s3_bucket, "
            "offsite_s3_access_key_id, offsite_s3_secret_access_key in credentials.yaml "
            f"(missing {', '.join(missing_o)})"
        )
    return None


def render_rclone_offsite_env(env: dict[str, str], *, project_name: str) -> str:
    """Render EnvironmentFile content for the offsite rclone oneshot."""
    region = (env.get("PGBR_S3_WRITE_REGION") or "").strip() or "garage"
    lines = [
        f"# Managed by {project_name} deploy (dk <env> dc-backup offsite install).",
        f"GARAGE_BUCKET={(env.get('PGBR_S3_WRITE_BUCKET') or '').strip()}",
        f"GARAGE_ACCESS_KEY_ID={(env.get('PGBR_S3_WRITE_KEY') or '').strip()}",
        f"GARAGE_SECRET_ACCESS_KEY={(env.get('PGBR_S3_WRITE_SECRET') or '').strip()}",
        f"GARAGE_ENDPOINT={GARAGE_LOOPBACK_ENDPOINT}",
        f"GARAGE_REGION={region}",
        f"OFFSITE_S3_BUCKET={(env.get('OFFSITE_S3_BUCKET') or '').strip()}",
        f"OFFSITE_S3_ACCESS_KEY_ID={(env.get('OFFSITE_S3_ACCESS_KEY_ID') or '').strip()}",
        f"OFFSITE_S3_SECRET_ACCESS_KEY={(env.get('OFFSITE_S3_SECRET_ACCESS_KEY') or '').strip()}",
        f"OFFSITE_S3_REGION={(env.get('OFFSITE_S3_REGION') or '').strip()}",
        f"OFFSITE_S3_ENDPOINT={(env.get('OFFSITE_S3_ENDPOINT') or '').strip()}",
        f"OFFSITE_S3_PREFIX={(env.get('OFFSITE_S3_PREFIX') or '').strip()}",
        f"OFFSITE_S3_PROVIDER={(env.get('OFFSITE_S3_PROVIDER') or '').strip() or 'Other'}",
    ]
    extra = (env.get("RCLONE_EXTRA_ARGS") or "").strip()
    if extra:
        lines.append(f"RCLONE_EXTRA_ARGS={extra}")
    image = (env.get("RCLONE_IMAGE") or "").strip()
    if image:
        lines.append(f"RCLONE_IMAGE={image}")
    return "\n".join(lines) + "\n"


def _read_info(config: ProjectConfig, env_name: str) -> dict[str, Any]:
    info_path = config.deploy_envs_dir / env_name / "info.yaml"
    if not info_path.is_file():
        raise FileNotFoundError(f"Missing {info_path}")
    with open(info_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _load_cred_env(config: ProjectConfig, env_name: str) -> tuple[dict[str, str], Path] | tuple[None, Path]:
    creds_path = config.deploy_envs_dir / env_name / "credentials.yaml"
    if not creds_path.is_file():
        print(f"Missing {creds_path}", file=sys.stderr)
        return None, creds_path
    try:
        ensure_sops_available()
        env = _credentials_to_env(decrypt_credentials_yaml(creds_path))
    except (SopsNotFoundError, SopsCommandError) as e:
        print(str(e), file=sys.stderr)
        return None, creds_path
    return env, creds_path


def _backup_ssh_target(config: ProjectConfig, env_name: str) -> str | None:
    try:
        info = _read_info(config, env_name)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return None
    backup_host = str(info.get(INFO_DC_BACKUP_DOCKER_HOST, "") or "").strip()
    if not backup_host:
        print(
            f"{INFO_DC_BACKUP_DOCKER_HOST} is unset in docker/envs/{env_name}/info.yaml.",
            file=sys.stderr,
        )
        return None
    try:
        return parse_docker_host_to_ssh_target(backup_host)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return None


def _ensure_known_host(config: ProjectConfig, env_name: str) -> int:
    info = _read_info(config, env_name)
    backup_host = str(info.get(INFO_DC_BACKUP_DOCKER_HOST, "") or "").strip()
    dh = backup_host if "://" in backup_host else f"ssh://{backup_host}"
    return ensure_ssh_known_host_for_docker_host(dh)


def cmd_dc_backup_offsite_install(
    config: ProjectConfig,
    env_name: str,
    *,
    enable: bool,
    yes: bool,
    dry_run: bool,
) -> int:
    """Install rclone offsite script, env file, and systemd units on the backup host."""
    ssh = _backup_ssh_target(config, env_name)
    if not ssh:
        return 1

    loaded = _load_cred_env(config, env_name)
    env, _creds_path = loaded
    if env is None:
        return 1

    err = validate_offsite_env(env)
    if err:
        print(err, file=sys.stderr)
        return 1

    install_prefix = (config.ops.install_prefix or "").strip().rstrip("/")
    config_dir = (config.ops.config_dir or "").strip().rstrip("/")
    if not install_prefix or not config_dir:
        print("ops.install_prefix and ops.config_dir are required in tooling.yaml.", file=sys.stderr)
        return 1

    service_name, timer_name = offsite_unit_names(config)
    env_body = render_rclone_offsite_env(env, project_name=config.meta.name)
    script_path = systemd_source_dir() / SCRIPT_FILENAME
    if not script_path.is_file():
        print(f"Missing bundled script: {script_path}", file=sys.stderr)
        return 1
    script_text = script_path.read_text(encoding="utf-8")

    try:
        service_body = render_systemd_unit(
            service_name,
            install_prefix=install_prefix,
            config_dir=config_dir,
        )
        timer_body = render_systemd_unit(
            timer_name,
            install_prefix=install_prefix,
            config_dir=config_dir,
        )
    except (ValueError, FileNotFoundError) as e:
        print(str(e), file=sys.stderr)
        return 1

    print(f"SSH target (dc_backup_docker_host): {ssh}", flush=True)
    print(
        f"Would install {SCRIPT_FILENAME}, {ENV_FILENAME}, {service_name}, {timer_name} "
        f"(enable={enable} dry_run={dry_run})",
        flush=True,
    )

    if dry_run:
        print(f"--- {config_dir}/{ENV_FILENAME} (redacted) ---", flush=True)
        print(redact_env_file_content(env_body), end="", flush=True)
        print("(dry-run) No files copied; no remote commands run.", flush=True)
        return 0

    if enable and not yes and not sys.stdin.isatty():
        print(
            "Refusing --enable without a TTY. Pass --yes for non-interactive use.",
            file=sys.stderr,
        )
        return 1

    if enable and sys.stdin.isatty() and not yes:
        print(
            "About to enable --now the offsite rclone timer on the DC backup host.",
            file=sys.stderr,
        )
        print(f"  Environment: {env_name}", file=sys.stderr)
        if not confirm_by_typing_env_name(env_name):
            print("Cancelled.", file=sys.stderr)
            return 1

    kh = _ensure_known_host(config, env_name)
    if kh != 0:
        print("Could not register SSH host key for backup host.", file=sys.stderr)
        return 1

    rc = remote_run(
        ssh,
        f"sudo mkdir -p {shlex.quote(install_prefix)} {shlex.quote(config_dir)} {REMOTE_SYSTEMD}",
        dry_run=False,
    )
    if rc != 0:
        return rc

    rc = install_files_via_ssh(
        ssh,
        install_prefix,
        [(SCRIPT_FILENAME, script_text, 0o755)],
        dry_run=False,
    )
    if rc != 0:
        return rc

    rc = install_files_via_ssh(
        ssh,
        config_dir,
        [(ENV_FILENAME, env_body, 0o600)],
        dry_run=False,
    )
    if rc != 0:
        return rc

    rc = install_files_via_ssh(
        ssh,
        REMOTE_SYSTEMD,
        [
            (service_name, service_body, 0o644),
            (timer_name, timer_body, 0o644),
        ],
        dry_run=False,
    )
    if rc != 0:
        return rc

    rc = remote_run(ssh, "sudo systemctl daemon-reload", dry_run=False)
    if rc != 0:
        return rc

    if enable:
        rc = remote_run(
            ssh,
            f"sudo systemctl enable --now {shlex.quote(timer_name)}",
            dry_run=False,
        )
        if rc != 0:
            return rc
        print(f"Enabled and started {timer_name}.", flush=True)
    else:
        print(
            f"Installed. Enable with: ssh {ssh} 'sudo systemctl enable --now {timer_name}' "
            f"(or re-run with --enable).",
            flush=True,
        )

    print(
        f"Next: `dk {env_name} dc-backup offsite run` for a one-shot copy, or wait for "
        f"05:00 (backup host local time). "
        f"Restore from offsite via pgbr_s3_read_* / restic_read_* (see README_DC_BACKUP.md).",
        flush=True,
    )
    return 0


def cmd_dc_backup_offsite_run(
    config: ProjectConfig,
    env_name: str,
    *,
    dry_run: bool,
) -> int:
    """One-shot rclone offsite copy on the backup host (install first)."""
    ssh = _backup_ssh_target(config, env_name)
    if not ssh:
        return 1

    service_name, _timer_name = offsite_unit_names(config)
    if dry_run:
        print(
            f"[dry-run] would refresh {ENV_FILENAME} and "
            f"ssh {ssh} sudo systemctl start {service_name}",
            flush=True,
        )
        return 0

    kh = _ensure_known_host(config, env_name)
    if kh != 0:
        return 1

    loaded = _load_cred_env(config, env_name)
    env, _ = loaded
    if env is None:
        return 1
    err = validate_offsite_env(env)
    if err:
        print(err, file=sys.stderr)
        return 1

    config_dir = (config.ops.config_dir or "").strip().rstrip("/")
    env_body = render_rclone_offsite_env(env, project_name=config.meta.name)
    rc = install_files_via_ssh(
        ssh,
        config_dir,
        [(ENV_FILENAME, env_body, 0o600)],
        dry_run=False,
    )
    if rc != 0:
        return rc

    result = remote_run_capture(
        ssh,
        f"sudo systemctl start {shlex.quote(service_name)}",
        dry_run=False,
    )
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        print(
            f"systemctl start {service_name} failed (exit {result.returncode}). "
            f"Check: ssh {ssh} 'journalctl -u {service_name} -n 50 --no-pager'",
            file=sys.stderr,
        )
        return int(result.returncode or 1)
    print(f"Started {service_name} (oneshot). Check journalctl on the backup host.", flush=True)
    return 0


def cmd_dc_backup_offsite_status(
    config: ProjectConfig,
    env_name: str,
) -> int:
    """Show timer/service state on the backup host."""
    ssh = _backup_ssh_target(config, env_name)
    if not ssh:
        return 1

    service_name, timer_name = offsite_unit_names(config)
    kh = _ensure_known_host(config, env_name)
    if kh != 0:
        return 1

    for unit in (timer_name, service_name):
        result = remote_run_capture(
            ssh,
            "systemctl show "
            + shlex.quote(unit)
            + " -p Id -p ActiveState -p SubState -p Result -p LastTriggerUSec -p ExecMainStatus --no-pager",
            dry_run=False,
        )
        print(f"=== {unit} ===", flush=True)
        if result.returncode != 0:
            print(
                f"(systemctl show failed: exit {result.returncode})",
                file=sys.stderr,
            )
            if result.stderr:
                print(result.stderr, end="", file=sys.stderr)
            continue
        print(result.stdout or "(no output)", end="", flush=True)
    return 0
