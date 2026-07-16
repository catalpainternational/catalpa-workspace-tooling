"""Provision Garage bucket/key and WRITE credentials for closed-DC backup."""

from __future__ import annotations

import re
import secrets
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from catalpa_tooling.cli_confirm import confirm_yes_default_no
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dc_backup.paths import (
    DEFAULT_GARAGE_REGION,
    INFO_DC_BACKUP_DOCKER_HOST,
)
from catalpa_tooling.dc_backup.ssh_install import remote_run_capture
from catalpa_tooling.dc_backup.stack import KEY_REGION, dc_backup_path
from catalpa_tooling.dc_backup.tls import (
    KEY_SERVER_DNS,
    KEY_SERVER_IPS,
    dc_backup_tls_path,
)
from catalpa_tooling.doctl_spaces_provision import (
    pgbr_write_configured,
    restic_write_configured,
)
from catalpa_tooling.env_yaml import _credentials_to_env
from catalpa_tooling.pgbackrest_volume_config import conflict_error_message
from catalpa_tooling.restic_files import restic_credentials_conflict_message
from catalpa_tooling.sops_credentials import (
    SopsCommandError,
    SopsNotFoundError,
    apply_credential_sets,
    decrypt_credentials_yaml,
    decrypt_sops_yaml,
    ensure_sops_available,
)
from catalpa_tooling.ssh_known_hosts import ensure_ssh_known_host_for_docker_host
from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

KEY_LAYOUT_CAPACITY = "garage_layout_capacity"
DEFAULT_LAYOUT_CAPACITY = "300G"
DEFAULT_LAYOUT_ZONE = "dc"

_BUCKET_EXISTS_RE = re.compile(
    r"already\s+exists|BucketAlreadyExists|BucketAlreadyOwnedByYou",
    re.IGNORECASE,
)
_KEY_EXISTS_RE = re.compile(r"already\s+exists|duplicate|KeyAlreadyExists", re.IGNORECASE)
_NO_ROLE_RE = re.compile(r"NO ROLE ASSIGNED", re.IGNORECASE)
_NODE_ID_RE = re.compile(r"^([0-9a-f]{8,})\s+\S+", re.IGNORECASE | re.MULTILINE)
_KEY_ID_RE = re.compile(
    r"^(?:Key ID|Access Key ID)\s*:\s*(\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SECRET_KEY_RE = re.compile(
    r"^(?:Secret key|Secret Access Key)\s*:\s*(\S+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SPACES_ENDPOINT_RE = re.compile(r"digitaloceanspaces\.com", re.IGNORECASE)


@dataclass(frozen=True)
class GarageBackupDefaults:
    endpoint: str
    region: str
    bucket: str
    key_name: str
    pgbackrest_repo_path: str
    restic_path: str
    stanza: str
    capacity: str


@dataclass(frozen=True)
class GarageAccessKey:
    access_key_id: str
    secret_access_key: str


class GarageProvisionError(Exception):
    """Remote Garage CLI or parsing failure."""


def _read_info(config: ProjectConfig, env_name: str) -> dict[str, Any]:
    info_path = config.deploy_envs_dir / env_name / "info.yaml"
    if not info_path.is_file():
        raise FileNotFoundError(f"Missing {info_path}")
    with open(info_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _host_from_docker_host(docker_host: str) -> str:
    """Extract hostname/IP from ``ssh://user@host`` or ``user@host``."""
    raw = (docker_host or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        return host
    if "@" in raw:
        return raw.rsplit("@", 1)[-1].strip()
    return raw


def garage_backup_defaults(
    config: ProjectConfig,
    env_name: str,
    *,
    endpoint: str | None = None,
    bucket: str | None = None,
    key_name: str | None = None,
    pgbr_repo_path: str | None = None,
    restic_prefix: str | None = None,
    capacity: str | None = None,
    region: str | None = None,
    info: dict[str, Any] | None = None,
    tls_data: dict[str, Any] | None = None,
    dc_backup_data: dict[str, Any] | None = None,
) -> GarageBackupDefaults:
    """Resolved Garage targets from project/env + optional TLS / dc-backup SOPS."""
    project = config.meta.name
    info = info if info is not None else {}
    tls_data = tls_data if tls_data is not None else {}
    dc_backup_data = dc_backup_data if dc_backup_data is not None else {}

    resolved_endpoint = (endpoint or "").strip()
    if not resolved_endpoint:
        ips = tls_data.get(KEY_SERVER_IPS) or []
        if isinstance(ips, list) and ips:
            resolved_endpoint = str(ips[0]).strip()
    if not resolved_endpoint:
        dns_names = tls_data.get(KEY_SERVER_DNS) or []
        if isinstance(dns_names, list) and dns_names:
            resolved_endpoint = str(dns_names[0]).strip()
    if not resolved_endpoint:
        backup_host = str(info.get(INFO_DC_BACKUP_DOCKER_HOST, "") or "").strip()
        resolved_endpoint = _host_from_docker_host(backup_host)

    resolved_region = (
        (region or "").strip()
        or str(dc_backup_data.get(KEY_REGION) or "").strip()
        or DEFAULT_GARAGE_REGION
    )
    resolved_bucket = (bucket or "").strip() or f"{project}-backups"
    resolved_key = (key_name or "").strip() or f"{project}-{env_name}-backup"
    resolved_pgbr = (pgbr_repo_path or "").strip() or f"/{project}/{env_name}/pgbackrest"
    resolved_restic = (restic_prefix or "").strip() or f"{project}-{env_name}-media"
    resolved_capacity = (
        (capacity or "").strip()
        or str(dc_backup_data.get(KEY_LAYOUT_CAPACITY) or "").strip()
        or DEFAULT_LAYOUT_CAPACITY
    )

    return GarageBackupDefaults(
        endpoint=resolved_endpoint,
        region=resolved_region,
        bucket=resolved_bucket,
        key_name=resolved_key,
        pgbackrest_repo_path=resolved_pgbr,
        restic_path=resolved_restic,
        stanza="main",
        capacity=resolved_capacity,
    )


def looks_like_spaces_endpoint(endpoint: str) -> bool:
    return bool(_SPACES_ENDPOINT_RE.search(endpoint or ""))


def write_credentials_look_like_spaces(env: dict[str, str]) -> bool:
    """True when existing WRITE credentials point at DigitalOcean Spaces."""
    for key in (
        "PGBR_S3_WRITE_ENDPOINT",
        "RESTIC_WRITE_REPOSITORY",
        "RESTIC_REPOSITORY",
    ):
        if looks_like_spaces_endpoint(env.get(key) or ""):
            return True
    return False


def parse_garage_key_info(text: str) -> GarageAccessKey:
    """Parse Key ID + secret from ``garage key create`` / ``key info`` output."""
    key_m = _KEY_ID_RE.search(text or "")
    sec_m = _SECRET_KEY_RE.search(text or "")
    if not key_m or not sec_m:
        raise GarageProvisionError(
            "Could not parse Garage Key ID / Secret key from CLI output. "
            "If the key already exists, try a new --key-name (secret may be hidden)."
        )
    return GarageAccessKey(access_key_id=key_m.group(1), secret_access_key=sec_m.group(1))


def parse_garage_node_id(status_text: str) -> str | None:
    """First healthy-node ID from ``garage status`` (full or prefix-usable)."""
    m = _NODE_ID_RE.search(status_text or "")
    return m.group(1) if m else None


def needs_layout_assign(status_text: str) -> bool:
    return bool(_NO_ROLE_RE.search(status_text or ""))


def parse_restic_s3_repository(repository: str) -> tuple[str, str, str] | None:
    """Parse ``s3:ENDPOINT/BUCKET/PREFIX`` into endpoint, bucket, prefix."""
    raw = (repository or "").strip()
    if not raw.startswith("s3:"):
        return None
    body = raw[3:]
    if body.startswith("//"):
        body = body[2:]
    parts = [p for p in body.split("/") if p]
    if len(parts) < 2:
        return None
    endpoint, bucket = parts[0], parts[1]
    prefix = "/".join(parts[2:]) if len(parts) > 2 else ""
    return endpoint, bucket, prefix


def build_credential_values(
    defaults: GarageBackupDefaults,
    access: GarageAccessKey,
    *,
    restic_password: str | None = None,
) -> dict[str, str]:
    """Full ``pgbr_s3_write_*`` + ``restic_write_*`` credential mapping."""
    password = (restic_password or "").strip() or secrets.token_urlsafe(32)
    repository = f"s3:{defaults.endpoint}/{defaults.bucket}/{defaults.restic_path}"
    return {
        "pgbr_s3_write_bucket": defaults.bucket,
        "pgbr_s3_write_region": defaults.region,
        "pgbr_s3_write_endpoint": defaults.endpoint,
        "pgbr_s3_write_uri_style": "path",
        "pgbr_s3_write_verify_tls": "y",
        "pgbr_s3_write_key": access.access_key_id,
        "pgbr_s3_write_secret": access.secret_access_key,
        "pgbr_s3_write_repo_path": defaults.pgbackrest_repo_path,
        "pgbr_s3_write_stanza": defaults.stanza,
        "restic_write_repository": repository,
        "restic_write_password": password,
        "restic_write_s3_access_key_id": access.access_key_id,
        "restic_write_s3_secret_access_key": access.secret_access_key,
        "restic_write_s3_default_region": defaults.region,
    }


def format_credential_yaml(values: dict[str, str]) -> str:
    return yaml.safe_dump(values, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _garage_remote_cmd(*args: str) -> str:
    quoted = " ".join(shlex.quote(a) for a in args)
    return f"sudo docker exec garage /garage {quoted}"


def _combined_output(result: Any) -> str:
    return f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"


def ensure_garage_layout(
    ssh: str,
    *,
    capacity: str,
    dry_run: bool,
) -> None:
    """Assign + apply single-node layout when status shows no role."""
    status = remote_run_capture(ssh, _garage_remote_cmd("status"), dry_run=dry_run)
    if dry_run:
        print(
            f"[dry-run] would check garage layout "
            f"(assign -z {DEFAULT_LAYOUT_ZONE} -c {capacity} if needed)",
            flush=True,
        )
        return
    if status.returncode != 0:
        raise GarageProvisionError(
            f"garage status failed (exit {status.returncode}): {_combined_output(status).strip()}"
        )
    text = _combined_output(status)
    if not needs_layout_assign(text):
        return
    node_id = parse_garage_node_id(text)
    if not node_id:
        raise GarageProvisionError(
            "garage status shows NO ROLE ASSIGNED but no node ID could be parsed."
        )
    assign = remote_run_capture(
        ssh,
        _garage_remote_cmd(
            "layout",
            "assign",
            "-z",
            DEFAULT_LAYOUT_ZONE,
            "-c",
            capacity,
            node_id,
        ),
        dry_run=False,
    )
    if assign.returncode != 0:
        raise GarageProvisionError(
            f"garage layout assign failed: {_combined_output(assign).strip()}"
        )
    apply = remote_run_capture(
        ssh,
        _garage_remote_cmd("layout", "apply", "--version", "1"),
        dry_run=False,
    )
    if apply.returncode != 0:
        raise GarageProvisionError(
            f"garage layout apply failed: {_combined_output(apply).strip()}"
        )


def ensure_garage_bucket(ssh: str, bucket: str, *, dry_run: bool) -> None:
    result = remote_run_capture(
        ssh,
        _garage_remote_cmd("bucket", "create", bucket),
        dry_run=dry_run,
    )
    if dry_run or result.returncode == 0:
        return
    combined = _combined_output(result)
    if _BUCKET_EXISTS_RE.search(combined):
        print(f"Garage bucket {bucket!r} already exists; reusing.", flush=True)
        return
    raise GarageProvisionError(
        f"garage bucket create {bucket!r} failed: {combined.strip()}"
    )


def ensure_garage_key(ssh: str, key_name: str, *, dry_run: bool) -> GarageAccessKey:
    """Create key or reuse via ``key info --show-secret`` when already present."""
    if dry_run:
        print(f"[dry-run] would create/reuse Garage key {key_name!r}", flush=True)
        return GarageAccessKey("GK_DRY_RUN", "dry-run-secret")

    created = remote_run_capture(
        ssh,
        _garage_remote_cmd("key", "create", key_name),
        dry_run=False,
    )
    if created.returncode == 0:
        return parse_garage_key_info(_combined_output(created))

    combined = _combined_output(created)
    already = bool(_KEY_EXISTS_RE.search(combined)) or "already" in combined.lower()

    for show_secret_flag in (("--show-secret",), ()):
        info = remote_run_capture(
            ssh,
            _garage_remote_cmd("key", "info", key_name, *show_secret_flag),
            dry_run=False,
        )
        if info.returncode != 0:
            continue
        try:
            return parse_garage_key_info(_combined_output(info))
        except GarageProvisionError:
            continue

    if already:
        raise GarageProvisionError(
            f"Garage key {key_name!r} already exists but the secret is not printable. "
            f"Pass a new --key-name to rotate, or delete the key on the backup host."
        )
    raise GarageProvisionError(
        f"garage key create {key_name!r} failed: {combined.strip()}"
    )


def allow_garage_bucket_key(
    ssh: str,
    bucket: str,
    key_name: str,
    *,
    dry_run: bool,
) -> None:
    result = remote_run_capture(
        ssh,
        _garage_remote_cmd(
            "bucket",
            "allow",
            "--read",
            "--write",
            "--owner",
            bucket,
            "--key",
            key_name,
        ),
        dry_run=dry_run,
    )
    if dry_run or result.returncode == 0:
        return
    raise GarageProvisionError(
        f"garage bucket allow failed: {_combined_output(result).strip()}"
    )


def provision_garage_access(
    ssh: str,
    defaults: GarageBackupDefaults,
    *,
    dry_run: bool,
) -> GarageAccessKey:
    ensure_garage_layout(ssh, capacity=defaults.capacity, dry_run=dry_run)
    ensure_garage_bucket(ssh, defaults.bucket, dry_run=dry_run)
    access = ensure_garage_key(ssh, defaults.key_name, dry_run=dry_run)
    allow_garage_bucket_key(ssh, defaults.bucket, defaults.key_name, dry_run=dry_run)
    return access


def _access_from_pgbr_env(env: dict[str, str]) -> GarageAccessKey | None:
    key = (env.get("PGBR_S3_WRITE_KEY") or "").strip()
    secret = (env.get("PGBR_S3_WRITE_SECRET") or "").strip()
    if key and secret:
        return GarageAccessKey(key, secret)
    return None


def _access_from_restic_env(env: dict[str, str]) -> GarageAccessKey | None:
    key = (env.get("RESTIC_WRITE_S3_ACCESS_KEY_ID") or "").strip()
    secret = (env.get("RESTIC_WRITE_S3_SECRET_ACCESS_KEY") or "").strip()
    if key and secret:
        return GarageAccessKey(key, secret)
    return None


def _defaults_from_existing_pgbr(
    defaults: GarageBackupDefaults,
    env: dict[str, str],
) -> GarageBackupDefaults:
    endpoint = (env.get("PGBR_S3_WRITE_ENDPOINT") or "").strip() or defaults.endpoint
    region = (env.get("PGBR_S3_WRITE_REGION") or "").strip() or defaults.region
    bucket = (env.get("PGBR_S3_WRITE_BUCKET") or "").strip() or defaults.bucket
    repo_path = (env.get("PGBR_S3_WRITE_REPO_PATH") or "").strip() or defaults.pgbackrest_repo_path
    stanza = (env.get("PGBR_S3_WRITE_STANZA") or "").strip() or defaults.stanza
    return GarageBackupDefaults(
        endpoint=endpoint,
        region=region,
        bucket=bucket,
        key_name=defaults.key_name,
        pgbackrest_repo_path=repo_path,
        restic_path=defaults.restic_path,
        stanza=stanza,
        capacity=defaults.capacity,
    )


def _defaults_from_existing_restic(
    defaults: GarageBackupDefaults,
    env: dict[str, str],
) -> GarageBackupDefaults:
    repo = (env.get("RESTIC_WRITE_REPOSITORY") or "").strip()
    parsed = parse_restic_s3_repository(repo)
    endpoint = defaults.endpoint
    bucket = defaults.bucket
    restic_path = defaults.restic_path
    if parsed:
        endpoint, bucket, prefix = parsed
        if prefix:
            restic_path = prefix
    region = (env.get("RESTIC_WRITE_S3_DEFAULT_REGION") or "").strip() or defaults.region
    return GarageBackupDefaults(
        endpoint=endpoint,
        region=region,
        bucket=bucket,
        key_name=defaults.key_name,
        pgbackrest_repo_path=defaults.pgbackrest_repo_path,
        restic_path=restic_path,
        stanza=defaults.stanza,
        capacity=defaults.capacity,
    )


def _print_already_configured_summary(env: dict[str, str], *, env_name: str) -> None:
    endpoint = (env.get("PGBR_S3_WRITE_ENDPOINT") or "").strip()
    bucket = (env.get("PGBR_S3_WRITE_BUCKET") or "").strip()
    repo = (env.get("RESTIC_WRITE_REPOSITORY") or "").strip()
    print("WRITE credentials already configured; nothing to do.", flush=True)
    if endpoint:
        print(f"  pgbr endpoint: {endpoint}", flush=True)
    if bucket:
        print(f"  pgbr bucket: {bucket}", flush=True)
    if repo:
        print(f"  restic repository: {repo}", flush=True)
    print(
        "  Use --force to overwrite SOPS, or --print-only to mint/print a Garage key "
        "without writing credentials.",
        flush=True,
    )
    print(
        f"  Next: recreate `db` if CA mounts changed, then "
        f"`dk {env_name} db backup` / `dk {env_name} files backup`.",
        flush=True,
    )


def _provision_confirm(
    *,
    env_name: str,
    creds_path: Path,
    defaults: GarageBackupDefaults,
    yes: bool,
    force: bool,
) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print(
            "Non-interactive session: pass global --yes to write credentials, "
            "or use --print-only.",
            file=sys.stderr,
        )
        return False
    action = "Overwrite" if force else "Write"
    return confirm_yes_default_no(
        f"{action} Garage WRITE credentials in {creds_path}?\n"
        f'  bucket={defaults.bucket!r} key={defaults.key_name!r} '
        f"endpoint={defaults.endpoint!r} env={env_name!r} [y/N]: "
    )


def cmd_dc_backup_provision(
    config: ProjectConfig,
    env_name: str,
    *,
    dry_run: bool = False,
    yes: bool = False,
    print_only: bool = False,
    force: bool = False,
    bucket: str | None = None,
    key_name: str | None = None,
    endpoint: str | None = None,
    pgbr_repo_path: str | None = None,
    restic_prefix: str | None = None,
    capacity: str | None = None,
) -> int:
    """Create Garage bucket/key and optionally write ``credentials.yaml``."""
    try:
        info = _read_info(config, env_name)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    backup_host = str(info.get(INFO_DC_BACKUP_DOCKER_HOST, "") or "").strip()
    if not backup_host:
        print(
            f"{INFO_DC_BACKUP_DOCKER_HOST} is unset in docker/envs/{env_name}/info.yaml.",
            file=sys.stderr,
        )
        return 1

    creds_path = config.deploy_envs_dir / env_name / "credentials.yaml"
    if not print_only and not creds_path.is_file():
        print(f"Missing {creds_path}", file=sys.stderr)
        return 1

    env: dict[str, str] = {}
    if creds_path.is_file():
        try:
            ensure_sops_available()
            env = _credentials_to_env(decrypt_credentials_yaml(creds_path))
        except (SopsNotFoundError, SopsCommandError) as e:
            print(str(e), file=sys.stderr)
            return 1

    conflict = conflict_error_message(env) or restic_credentials_conflict_message(env)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1

    dc_data: dict[str, Any] = {}
    sops_stack = dc_backup_path(config, env_name)
    if sops_stack.is_file():
        try:
            dc_data = decrypt_sops_yaml(sops_stack)
        except (SopsNotFoundError, SopsCommandError) as e:
            print(str(e), file=sys.stderr)
            return 1

    tls_data: dict[str, Any] = {}
    tls_path = dc_backup_tls_path(config, env_name)
    if tls_path.is_file():
        try:
            tls_data = decrypt_sops_yaml(tls_path)
        except (SopsNotFoundError, SopsCommandError):
            tls_data = {}

    defaults = garage_backup_defaults(
        config,
        env_name,
        endpoint=endpoint,
        bucket=bucket,
        key_name=key_name,
        pgbr_repo_path=pgbr_repo_path,
        restic_prefix=restic_prefix,
        capacity=capacity,
        info=info,
        tls_data=tls_data,
        dc_backup_data=dc_data,
    )
    if not defaults.endpoint:
        print(
            "Could not resolve S3 endpoint. Pass --endpoint, or ensure "
            "dc-backup-tls.yaml has backup_server_ips / backup_server_dns.",
            file=sys.stderr,
        )
        return 1

    pgbr_ok = pgbr_write_configured(env)
    restic_ok = restic_write_configured(env)
    spaces_like = write_credentials_look_like_spaces(env)

    if pgbr_ok and restic_ok and not force and not print_only:
        _print_already_configured_summary(env, env_name=env_name)
        return 0

    if spaces_like and not force and not print_only:
        print(
            "Existing WRITE credentials look like DigitalOcean Spaces. "
            "Migrating to Garage requires --force (or use --print-only and paste manually).",
            file=sys.stderr,
        )
        return 1

    reuse_access: GarageAccessKey | None = None
    need_garage = True
    sops_keys: set[str] | None = None  # None = write all built keys

    if force or print_only or (not pgbr_ok and not restic_ok):
        need_garage = True
        sops_keys = None
    elif pgbr_ok and not restic_ok:
        reuse_access = _access_from_pgbr_env(env)
        if reuse_access:
            need_garage = False
            defaults = _defaults_from_existing_pgbr(defaults, env)
            sops_keys = {
                "restic_write_repository",
                "restic_write_password",
                "restic_write_s3_access_key_id",
                "restic_write_s3_secret_access_key",
                "restic_write_s3_default_region",
            }
        else:
            need_garage = True
            sops_keys = None
    elif restic_ok and not pgbr_ok:
        reuse_access = _access_from_restic_env(env)
        if reuse_access:
            need_garage = False
            defaults = _defaults_from_existing_restic(defaults, env)
            sops_keys = {
                "pgbr_s3_write_bucket",
                "pgbr_s3_write_region",
                "pgbr_s3_write_endpoint",
                "pgbr_s3_write_uri_style",
                "pgbr_s3_write_verify_tls",
                "pgbr_s3_write_key",
                "pgbr_s3_write_secret",
                "pgbr_s3_write_repo_path",
                "pgbr_s3_write_stanza",
            }
        else:
            need_garage = True
            sops_keys = None

    if dry_run:
        print(
            f"dry-run: would provision Garage bucket={defaults.bucket!r} "
            f"key={defaults.key_name!r} endpoint={defaults.endpoint!r}",
            flush=True,
        )
        if need_garage:
            print("dry-run: would run garage layout/bucket/key/allow on backup host", flush=True)
        else:
            print("dry-run: would reuse existing WRITE access keys (no Garage create)", flush=True)
        if print_only:
            print("dry-run: would print YAML fragment only (no SOPS write)", flush=True)
        else:
            which = "all WRITE keys" if sops_keys is None else ", ".join(sorted(sops_keys))
            print(f"dry-run: would sops set {which} in {creds_path}", flush=True)
        return 0

    try:
        ssh = parse_docker_host_to_ssh_target(backup_host)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    kh = ensure_ssh_known_host_for_docker_host(
        backup_host if "://" in backup_host else f"ssh://{backup_host}"
    )
    if kh != 0:
        print(f"Could not register SSH host key for {backup_host!r}.", file=sys.stderr)
        return 1

    try:
        if need_garage:
            access = provision_garage_access(ssh, defaults, dry_run=False)
        else:
            assert reuse_access is not None
            access = reuse_access
            print(
                f"Reusing existing WRITE access key for missing credential half "
                f"({access.access_key_id[:8]}…).",
                flush=True,
            )
    except GarageProvisionError as e:
        print(str(e), file=sys.stderr)
        return 1

    existing_restic_pw = (env.get("RESTIC_WRITE_PASSWORD") or "").strip() or None
    values = build_credential_values(
        defaults,
        access,
        restic_password=existing_restic_pw,
    )
    print(format_credential_yaml(values), end="", flush=True)

    if print_only:
        print(
            f"# Printed only; paste into {creds_path} via `dk {env_name} secrets` if desired.",
            flush=True,
        )
        print(
            f"# Next: recreate `db`, then `dk {env_name} db backup` / "
            f"`dk {env_name} files backup` (not auto-provisioned from those commands).",
            flush=True,
        )
        return 0

    if not _provision_confirm(
        env_name=env_name,
        creds_path=creds_path,
        defaults=defaults,
        yes=yes,
        force=force,
    ):
        print("Declined; credentials not written.", file=sys.stderr)
        print(
            f"Fragment was printed above; paste via `dk {env_name} secrets` "
            f"or re-run with --yes.",
            file=sys.stderr,
        )
        return 1

    to_write = values if sops_keys is None else {k: values[k] for k in sops_keys if k in values}
    try:
        ensure_sops_available()
        apply_credential_sets(creds_path, to_write)
    except (SopsNotFoundError, SopsCommandError) as e:
        print(str(e), file=sys.stderr)
        return getattr(e, "returncode", 1) or 1

    print(
        f"Updated {creds_path} with Garage WRITE credentials "
        f"(bucket {defaults.bucket!r}, endpoint {defaults.endpoint!r}).",
        flush=True,
    )
    print(
        f"Next: recreate the `db` service so CA/env mounts apply, then "
        f"`dk {env_name} db backup` / `dk {env_name} files backup` "
        f"(db/files do not call `dc-backup provision` automatically).",
        flush=True,
    )
    return 0
