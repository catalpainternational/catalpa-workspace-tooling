"""Auto-provision DigitalOcean Spaces credentials for ``bkp_db`` / ``bkp_files``."""

from __future__ import annotations

import re
import secrets
import sys
import time
from dataclasses import dataclass
from typing import Literal

from pathlib import Path

from catalpa_tooling.cli_confirm import confirm_yes_default_no
from catalpa_tooling.config import ProjectConfig, SpacesConfig
from catalpa_tooling.doctl_binary import (
    DoctlCommandError,
    DoctlNotFoundError,
    ensure_doctl_available,
    print_doctl_required,
    run_doctl,
    run_doctl_json,
)
from catalpa_tooling.pgbackrest_volume_config import (
    PREFIX_WRITE,
    _extract_stanza_vars,
    _validate_repo_vars,
    conflict_error_message,
    resolve_mode,
)
from catalpa_tooling.restic_files import (
    restic_credentials_conflict_message,
    restic_credential_mode,
    validate_restic_env,
)
from catalpa_tooling.s3cmd_binary import (
    S3cmdCommandError,
    S3cmdNotFoundError,
    ensure_s3cmd_available,
    print_s3cmd_required,
    run_s3cmd,
)
from catalpa_tooling.doctl_projects import list_project_resource_urns, resolve_project_id
from catalpa_tooling.sops_credentials import (
    SopsCommandError,
    SopsNotFoundError,
    apply_credential_sets,
    ensure_sops_available,
    refresh_env_credentials,
)

ProvisionTarget = Literal["pgbackrest", "restic"]

_BUCKET_EXISTS_RE = re.compile(
    r"bucket.*already\s+exists|BucketAlreadyOwnedByYou|BucketAlreadyExists",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SpacesBackupDefaults:
    endpoint: str
    region: str
    bucket: str
    pgbackrest_repo_path: str
    restic_path: str
    stanza: str
    write_key_name: str
    host_bucket: str


def spaces_backup_defaults(config: ProjectConfig, env_name: str) -> SpacesBackupDefaults:
    """Resolved Spaces targets from ``tooling.yaml`` and deploy env name."""
    do = config.digitalocean
    spaces: SpacesConfig | None = do.spaces if do else None
    region = (
        (spaces.region if spaces and spaces.region else None)
        or (do.region if do and do.region else None)
        or "sgp1"
    )
    endpoint = (
        (spaces.endpoint if spaces and spaces.endpoint else None)
        or f"{region}.digitaloceanspaces.com"
    )
    project = config.meta.name
    bucket = (spaces.bucket if spaces and spaces.bucket else None) or project
    pgbackrest_repo_path = (
        spaces.pgbackrest_repo_path if spaces and spaces.pgbackrest_repo_path else None
    ) or f"/{project}/{env_name}/pgbackrest"
    restic_path = (
        spaces.restic_path if spaces and spaces.restic_path else None
    ) or f"{project}-{env_name}-media"
    stanza = (spaces.stanza if spaces and spaces.stanza else None) or "main"
    return SpacesBackupDefaults(
        endpoint=endpoint,
        region=region,
        bucket=bucket,
        pgbackrest_repo_path=pgbackrest_repo_path,
        restic_path=restic_path,
        stanza=stanza,
        write_key_name=f"{bucket}-write",
        host_bucket=f"%(bucket)s.{region}.digitaloceanspaces.com",
    )


def needs_pgbr_write(sub: str, tail: list[str]) -> bool:
    """True when a ``bkp_db`` subcommand should auto-provision WRITE-mode S3 credentials.

    Restore and ``configure verify`` accept READ-mode credentials (``pgbr_s3_read_*``);
  they must not trigger Spaces bucket creation.
    """
    if sub in ("install-systemd", "backup", "init"):
        return True
    if sub == "configure" and tail == ["stanza-create"]:
        return True
    return False


def needs_restic_write(sub: str) -> bool:
    """True when a ``bkp_files`` subcommand should auto-provision WRITE-mode restic credentials."""
    return sub in ("install-systemd", "backup", "init")


def pgbr_write_configured(env: dict[str, str]) -> bool:
    if resolve_mode(env) != "write":
        return False
    vars_map = _extract_stanza_vars(env, PREFIX_WRITE)
    return _validate_repo_vars(vars_map, mode="write") is None


def restic_write_configured(env: dict[str, str]) -> bool:
    if restic_credential_mode(env) != "write":
        return False
    return validate_restic_env(env) is None


def _doctl_context(config: ProjectConfig) -> str | None:
    do = config.digitalocean
    return do.context if do else None


def _spaces_bucket_urn(bucket: str) -> str:
    return f"do:space:{bucket}"


def _resolve_digitalocean_project_id(
    config: ProjectConfig,
    *,
    context: str | None,
) -> str | None:
    do = config.digitalocean
    if not do or not (do.project_id or do.project_name):
        return None
    return resolve_project_id(None, do_config=do, context=context)


def _ensure_bucket_in_project(
    bucket: str,
    *,
    config: ProjectConfig,
    context: str | None,
    dry_run: bool,
) -> None:
    """Assign the Spaces bucket to ``digitalocean.project_name`` / ``project_id`` when configured."""
    project_id = _resolve_digitalocean_project_id(config, context=context)
    if not project_id:
        return

    urn = _spaces_bucket_urn(bucket)
    if not dry_run:
        existing = list_project_resource_urns(project_id, context=context)
        if urn in existing:
            return

    if dry_run:
        print(
            f"dry-run: would run doctl projects resources assign {project_id} "
            f"--resource={urn!r}",
            file=sys.stderr,
        )
        return

    result = run_doctl(
        ["projects", "resources", "assign", project_id, "--resource", urn],
        context=context,
    )
    if result.returncode != 0:
        combined = f"{result.stderr or ''}\n{result.stdout or ''}"
        err = combined.strip() or f"doctl assign failed (exit {result.returncode})"
        raise DoctlCommandError(
            f"Failed to assign Spaces bucket {bucket!r} to DigitalOcean project: {err}",
            returncode=result.returncode,
        )


def _s3cmd_spaces_args(
    defaults: SpacesBackupDefaults, *, access_key: str, secret_key: str
) -> list[str]:
    return [
        f"--access_key={access_key}",
        f"--secret_key={secret_key}",
        f"--host={defaults.endpoint}",
        f"--host-bucket={defaults.host_bucket}",
        f"--region={defaults.region}",
    ]


def _normalize_key_record(item: dict) -> tuple[str, str, str]:
    """Return ``(name, access_key, secret_key)`` from a doctl spaces key object."""
    lowered: dict[str, str] = {}
    for k, v in item.items():
        if isinstance(k, str) and v is not None:
            lowered[k.lower()] = str(v).strip()
    name = lowered.get("name", "")
    access = lowered.get("access_key") or lowered.get("accesskey") or ""
    secret = lowered.get("secret_key") or lowered.get("secretkey") or ""
    return name, access, secret


def _parse_created_spaces_key(data: object) -> tuple[str, str]:
    if isinstance(data, list):
        if not data:
            raise DoctlCommandError("doctl returned empty key list", returncode=1)
        item = data[0]
    elif isinstance(data, dict):
        item = data
    else:
        raise DoctlCommandError(f"unexpected doctl key payload: {type(data)!r}", returncode=1)
    if not isinstance(item, dict):
        raise DoctlCommandError("unexpected doctl key entry type", returncode=1)
    _, access, secret = _normalize_key_record(item)
    if not access or not secret:
        raise DoctlCommandError(
            "doctl spaces keys create did not return access_key and secret_key",
            returncode=1,
        )
    return access, secret


def _spaces_key_name_exists(name: str, *, context: str | None) -> bool:
    data = run_doctl_json(
        ["spaces", "keys", "list", "--format", "Name", "--no-header"],
        context=context,
    )
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                row_name = str(row.get("name") or row.get("Name") or "").strip()
            else:
                row_name = str(row).strip()
            if row_name == name:
                return True
    return False


def _create_spaces_key(
    name: str,
    grants: str,
    *,
    context: str | None,
    dry_run: bool,
) -> tuple[str, str]:
    if dry_run:
        print(
            f"dry-run: would run doctl spaces keys create {name!r} --grants {grants!r}",
            file=sys.stderr,
        )
        return ("DRYRUN_ACCESS_KEY", "DRYRUN_SECRET_KEY")
    if _spaces_key_name_exists(name, context=context):
        print(
            f"Spaces access key named {name!r} already exists. "
            "Delete it in the DigitalOcean control panel (or via "
            f"`doctl spaces keys delete <access-key>`) and re-run, "
            "or set credentials manually with `dk <env> secrets`.",
            file=sys.stderr,
        )
        raise DoctlCommandError(f"spaces key {name!r} already exists", returncode=1)
    data = run_doctl_json(
        ["spaces", "keys", "create", name, "--grants", grants],
        context=context,
    )
    return _parse_created_spaces_key(data)


def _delete_spaces_key(access_key: str, *, context: str | None, dry_run: bool) -> None:
    if dry_run:
        print(
            f"dry-run: would run doctl spaces keys delete {access_key!r}",
            file=sys.stderr,
        )
        return
    result = run_doctl(["spaces", "keys", "delete", access_key], context=context)
    if result.returncode != 0:
        print(
            f"Warning: could not delete temporary Spaces key {access_key!r} "
            f"(exit {result.returncode}). Remove it manually if needed.",
            file=sys.stderr,
        )


def _bucket_exists(
    defaults: SpacesBackupDefaults,
    *,
    access_key: str,
    secret_key: str,
    dry_run: bool,
) -> bool:
    if dry_run:
        print(
            f"dry-run: would run s3cmd info s3://{defaults.bucket} "
            f"(host {defaults.endpoint})",
            file=sys.stderr,
        )
        return False
    result = run_s3cmd(
        ["info", f"s3://{defaults.bucket}", *_s3cmd_spaces_args(defaults, access_key=access_key, secret_key=secret_key)],
        capture_output=True,
    )
    return result.returncode == 0


def _create_bucket(
    defaults: SpacesBackupDefaults,
    *,
    access_key: str,
    secret_key: str,
    dry_run: bool,
) -> None:
    if dry_run:
        print(
            f"dry-run: would run s3cmd mb s3://{defaults.bucket} (host {defaults.endpoint})",
            file=sys.stderr,
        )
        return
    result = run_s3cmd(
        ["mb", f"s3://{defaults.bucket}", *_s3cmd_spaces_args(defaults, access_key=access_key, secret_key=secret_key)],
        capture_output=True,
    )
    if result.returncode == 0:
        return
    combined = f"{result.stderr or ''}\n{result.stdout or ''}"
    if _BUCKET_EXISTS_RE.search(combined):
        return
    err = (result.stderr or result.stdout or "").strip() or f"s3cmd mb failed (exit {result.returncode})"
    raise S3cmdCommandError(err, returncode=result.returncode)


def _ensure_bucket(
    defaults: SpacesBackupDefaults,
    *,
    config: ProjectConfig,
    context: str | None,
    env_name: str,
    dry_run: bool,
) -> None:
    """Create the Spaces bucket when it does not exist (bootstrap key + s3cmd)."""
    bootstrap_name = f"dk-bootstrap-{env_name}-{int(time.time())}"
    access, secret = _create_spaces_key(
        bootstrap_name,
        "permission=fullaccess",
        context=context,
        dry_run=dry_run,
    )
    try:
        if not _bucket_exists(defaults, access_key=access, secret_key=secret, dry_run=dry_run):
            _create_bucket(defaults, access_key=access, secret_key=secret, dry_run=dry_run)
        _ensure_bucket_in_project(
            defaults.bucket,
            config=config,
            context=context,
            dry_run=dry_run,
        )
    finally:
        if not dry_run:
            _delete_spaces_key(access, context=context, dry_run=dry_run)


def _pgbr_keys_from_env(env: dict[str, str]) -> tuple[str, str] | None:
    key = (env.get("PGBR_S3_WRITE_KEY") or "").strip()
    secret = (env.get("PGBR_S3_WRITE_SECRET") or "").strip()
    if key and secret:
        return key, secret
    return None


def _provision_prompt(
    *,
    target: ProvisionTarget,
    env_name: str,
    creds_path: Path,
    defaults: SpacesBackupDefaults,
    yes: bool,
) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        print(
            "Backup credentials are missing and this session is non-interactive. "
            "Run from a TTY or pass global --yes to auto-provision.",
            file=sys.stderr,
        )
        return False
    label = "pgBackRest S3" if target == "pgbackrest" else "restic"
    return confirm_yes_default_no(
        f"No {label} credentials in {creds_path}.\n"
        f'Create DigitalOcean Spaces bucket "{defaults.bucket}" and write key '
        f'"{defaults.write_key_name}"? [y/N]: '
    )


def ensure_spaces_backup_credentials(
    config: ProjectConfig,
    env_name: str,
    env_add: dict[str, str],
    creds_path: Path,
    *,
    target: ProvisionTarget,
    command_label: str,
    dry_run: bool = False,
    yes: bool = False,
) -> int:
    """Provision missing WRITE credentials; return 0 on success or when already configured."""
    if target == "pgbackrest" and pgbr_write_configured(env_add):
        return 0
    if target == "restic" and restic_write_configured(env_add):
        return 0

    if target == "pgbackrest":
        conflict = conflict_error_message(env_add)
    else:
        conflict = restic_credentials_conflict_message(env_add)
    if conflict:
        print(conflict, file=sys.stderr)
        return 1

    defaults = spaces_backup_defaults(config, env_name)
    context = _doctl_context(config)

    if not creds_path.is_file():
        print(f"Missing {creds_path}", file=sys.stderr)
        return 1

    if not _provision_prompt(
        target=target,
        env_name=env_name,
        creds_path=creds_path,
        defaults=defaults,
        yes=yes,
    ):
        print(
            "Provisioning declined. Set credentials manually or run "
            f"`dk {env_name} secrets`.",
            file=sys.stderr,
        )
        return 1

    if dry_run:
        print(
            f"dry-run: would provision {target} Spaces credentials for {env_name} "
            f"(bucket={defaults.bucket!r}, key={defaults.write_key_name!r})",
            file=sys.stderr,
        )
        if target == "pgbackrest":
            print(
                f"dry-run: would sops set pgbr_s3_write_* in {creds_path}",
                file=sys.stderr,
            )
        else:
            print(
                f"dry-run: would sops set restic_write_* in {creds_path}",
                file=sys.stderr,
            )
        _ensure_bucket_in_project(
            defaults.bucket,
            config=config,
            context=context,
            dry_run=True,
        )
        return 0

    try:
        ensure_doctl_available()
    except DoctlNotFoundError as e:
        print(f"{command_label} requires doctl to create Spaces access keys.", file=sys.stderr)
        print_doctl_required(e)
        return 1

    reuse_pgbr_keys = target == "restic" and pgbr_write_configured(env_add)
    need_bucket_ops = not reuse_pgbr_keys

    if need_bucket_ops:
        try:
            ensure_s3cmd_available()
        except S3cmdNotFoundError as e:
            print(
                f"{command_label} requires s3cmd to create the Spaces bucket.",
                file=sys.stderr,
            )
            print_s3cmd_required(e)
            return 1

    try:
        ensure_sops_available()
    except SopsNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    access_key = ""
    secret_key = ""
    if reuse_pgbr_keys:
        pair = _pgbr_keys_from_env(env_add)
        assert pair is not None
        access_key, secret_key = pair
        print(
            f"Reusing pgBackRest Spaces access key for restic ({defaults.write_key_name}).",
            file=sys.stderr,
        )
    else:
        try:
            _ensure_bucket(
                defaults,
                config=config,
                context=context,
                env_name=env_name,
                dry_run=False,
            )
            access_key, secret_key = _create_spaces_key(
                defaults.write_key_name,
                f"bucket={defaults.bucket};permission=readwrite",
                context=context,
                dry_run=False,
            )
        except (DoctlCommandError, S3cmdCommandError) as e:
            print(str(e), file=sys.stderr)
            return getattr(e, "returncode", 1)

    values: dict[str, str] = {}
    if target == "pgbackrest":
        values = {
            "pgbr_s3_write_bucket": defaults.bucket,
            "pgbr_s3_write_region": defaults.region,
            "pgbr_s3_write_endpoint": defaults.endpoint,
            "pgbr_s3_write_repo_path": defaults.pgbackrest_repo_path,
            "pgbr_s3_write_stanza": defaults.stanza,
            "pgbr_s3_write_key": access_key,
            "pgbr_s3_write_secret": secret_key,
        }
    else:
        bucket = (env_add.get("PGBR_S3_WRITE_BUCKET") or "").strip() or defaults.bucket
        endpoint = (env_add.get("PGBR_S3_WRITE_ENDPOINT") or "").strip() or defaults.endpoint
        region = (env_add.get("PGBR_S3_WRITE_REGION") or "").strip() or defaults.region
        repository = f"s3:{endpoint}/{bucket}/{defaults.restic_path}"
        values = {
            "restic_write_repository": repository,
            "restic_write_password": secrets.token_urlsafe(32),
            "restic_write_s3_default_region": region,
            "restic_write_s3_access_key_id": access_key,
            "restic_write_s3_secret_access_key": secret_key,
        }

    try:
        apply_credential_sets(creds_path, values)
    except SopsCommandError as e:
        return e.returncode

    refresh_env_credentials(env_add, creds_path)
    print(
        f"Updated {creds_path} with {target} Spaces credentials (bucket {defaults.bucket!r}).",
        file=sys.stderr,
    )
    return 0
