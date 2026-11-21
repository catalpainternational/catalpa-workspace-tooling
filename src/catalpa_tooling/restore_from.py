"""Restore-only envs (#204, tooling 2.0 M5b): ``restore_from: <env>`` in ``info.yaml``.

An env that restores another env's backups holds read credentials for that env's repositories:
``pgbr_s3_read_*`` and ``restic_read_*``. ``dk <env> secrets read-key`` makes them: it reads the
source's non-secret settings (bucket, endpoint, region, repository path, stanza; the restic
repository and its password) from the source's ``credentials.yaml``, creates a Spaces key that
can only read the source's bucket(s), named ``<bucket>-<env>-read``, and writes the env's read
credentials with ``sops set``. Each env gets its own key, so one env's access can be revoked
without touching the others. No value is printed.

What the env then does with them is its runtime's: compose envs restore in READ mode as before;
a rendered (quadlet) env gets them as Podman secrets for its one-off restore containers.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from catalpa_tooling.config import ProjectConfig

#: The source's write settings → this env's read settings. Key and secret come from the new key.
PGBR_SETTINGS = ("bucket", "region", "endpoint", "repo_path", "stanza", "uri_style", "verify_tls")
RESTIC_SETTINGS = ("repository", "password", "s3_default_region")


class RestoreFromError(ValueError):
    pass


def restore_source(config: ProjectConfig, env_name: str, info: dict[str, Any]) -> str | None:
    """The env this one restores from, checked; None when ``restore_from`` isn't set."""
    raw = info.get("restore_from")
    if raw is None:
        return None
    source = str(raw).strip()
    if not source:
        raise RestoreFromError(f"{env_name}: restore_from is empty")
    if source == env_name:
        raise RestoreFromError(f"{env_name}: restore_from names the env itself")
    if not (config.deploy_envs_dir / source / "info.yaml").is_file():
        raise RestoreFromError(f"{env_name}: restore_from: {source!r} is not an env (no docker/envs/{source}/info.yaml)")
    return source


def read_key_name(bucket: str, env_name: str) -> str:
    return f"{bucket}-{env_name}-read"


_REPO_RE = re.compile(r"^s3:(?:https?://)?[^/]+/([^/]+)(?:/.*)?$")


def restic_bucket(repository: str) -> str | None:
    """The bucket of an ``s3:<endpoint>/<bucket>/<path>`` restic repository."""
    m = _REPO_RE.match(repository.strip())
    return m.group(1) if m else None


def read_values(source_creds: dict[str, Any], *, source: str) -> tuple[dict[str, str], list[str]]:
    """This env's read settings from the source's credentials, without the key, and the buckets
    the key must read. Refuses a source without backups of its own (write credentials)."""
    flat = {str(k).lower(): v for k, v in source_creds.items()}
    values: dict[str, str] = {}
    buckets: list[str] = []
    bucket = flat.get("pgbr_s3_write_bucket")
    if bucket:
        for s in PGBR_SETTINGS:
            v = flat.get(f"pgbr_s3_write_{s}")
            if v not in (None, ""):
                values[f"pgbr_s3_read_{s}"] = str(v)
        buckets.append(str(bucket))
    repo = flat.get("restic_write_repository")
    if repo:
        rb = restic_bucket(str(repo))
        if rb is None:
            raise RestoreFromError(f"{source}'s restic_write_repository isn't an s3: repository on Spaces")
        for s in RESTIC_SETTINGS:
            v = flat.get(f"restic_write_{s}")
            if v not in (None, ""):
                values[f"restic_read_{s}"] = str(v)
        if rb not in buckets:
            buckets.append(rb)
    if not buckets:
        raise RestoreFromError(
            f"{source} has no backups to restore from: its credentials.yaml has no "
            "pgbr_s3_write_* or restic_write_*"
        )
    return values, buckets


def with_key(values: dict[str, str], access: str, secret: str) -> dict[str, str]:
    out = dict(values)
    if "pgbr_s3_read_bucket" in out:
        out["pgbr_s3_read_key"] = access
        out["pgbr_s3_read_secret"] = secret
    if "restic_read_repository" in out:
        out["restic_read_s3_access_key_id"] = access
        out["restic_read_s3_secret_access_key"] = secret
    return out


def _has_read_credentials(creds: dict[str, Any]) -> bool:
    flat = {str(k).lower() for k, v in creds.items() if v not in (None, "")}
    return any(k.startswith(("pgbr_s3_read_", "restic_read_")) for k in flat)


def cmd_read_key(
    config: ProjectConfig,
    env_name: str,
    *,
    dry_run: bool,
    yes: bool,
    decrypt=None,
) -> int:
    """``dk <env> secrets read-key``."""
    from catalpa_tooling.deploy_do_link import load_env_info
    from catalpa_tooling.project_credentials import (
        credentials_for_env,
        env_credentials_path,
    )

    loaded = load_env_info(config, env_name)
    if loaded is None:
        return 1
    _path, info = loaded
    try:
        source = restore_source(config, env_name, info)
    except RestoreFromError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if source is None:
        print(
            f"{env_name} has no restore_from in info.yaml: set it to the env whose backups this "
            "one restores (e.g. `restore_from: prod`).",
            file=sys.stderr,
        )
        return 1
    creds_path = env_credentials_path(config, env_name)
    if not creds_path.is_file():
        print(f"Missing {creds_path}: create it with `dk {env_name} secrets` first.", file=sys.stderr)
        return 1
    try:
        own = credentials_for_env(config, env_name, decrypt=decrypt)
        if _has_read_credentials(own):
            print(
                f"{env_name} already has read credentials (pgbr_s3_read_* / restic_read_*). To "
                f"make a new key, remove them first (`sops unset {creds_path.relative_to(config.repo_root)} "
                "'[\"pgbr_s3_read_key\"]'`, and so on), then delete the old key.",
                file=sys.stderr,
            )
            return 1
        values, buckets = read_values(credentials_for_env(config, source, decrypt=decrypt), source=source)
    except RestoreFromError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    key_name = read_key_name(buckets[0], env_name)
    grants = ",".join(f"bucket={b};permission=read" for b in buckets)
    what = " and ".join(
        label for label, prefix in (("pgBackRest", "pgbr_s3_read_"), ("restic", "restic_read_"))
        if any(k.startswith(prefix) for k in values)
    )  # fmt: skip
    print(
        f"{env_name} restores from {source}: a Spaces key {key_name!r} that can only read "
        f"{', '.join(buckets)}, and {env_name}'s {what} read credentials from {source}'s settings, "
        f"into {creds_path.relative_to(config.repo_root)}.",
        file=sys.stderr,
    )
    if dry_run:
        print(f"dry-run: would run doctl spaces keys create {key_name!r} --grants {grants!r}", file=sys.stderr)
        print(f"dry-run: would sops set {', '.join(sorted(with_key(values, '', '')))}", file=sys.stderr)
        return 0
    if not yes:
        from catalpa_tooling.cli_confirm import confirm_yes_default_no

        if not sys.stdin.isatty() or not confirm_yes_default_no("Create the key and write the credentials? [y/N]: "):
            print("secrets read-key: cancelled.", file=sys.stderr)
            return 1
    return _create_and_write(config, env_name, creds_path, key_name, grants, values)


def _create_and_write(
    config: ProjectConfig, env_name: str, creds_path: Path, key_name: str, grants: str, values: dict[str, str]
) -> int:
    from catalpa_tooling.doctl_binary import DoctlCommandError, ensure_doctl_available
    from catalpa_tooling.doctl_spaces_provision import (
        _create_spaces_key,
        _delete_spaces_key,
        _doctl_context,
    )
    from catalpa_tooling.sops_credentials import SopsCommandError, apply_credential_sets

    ensure_doctl_available()
    context = _doctl_context(config)
    try:
        access, secret = _create_spaces_key(key_name, grants, context=context, dry_run=False)
    except DoctlCommandError as exc:
        print(str(exc), file=sys.stderr)
        return exc.returncode or 1
    try:
        apply_credential_sets(creds_path, with_key(values, access, secret))
    except SopsCommandError as exc:
        print("Writing the credentials failed: deleting the new key again.", file=sys.stderr)
        _delete_spaces_key(access, context=context, dry_run=False)
        return exc.returncode or 1
    rel = creds_path.relative_to(config.repo_root)
    print(f"Made key {key_name!r} and wrote {env_name}'s read credentials into {rel}.", file=sys.stderr)
    from catalpa_tooling.deploy_do_link import load_env_info

    loaded = load_env_info(config, env_name)
    if loaded and str(loaded[1].get("runtime") or "") == "quadlet":
        print(
            f"Next: `dk {env_name} render` and commit (the restore containers get the new secrets), "
            f"then `dk {env_name} secrets push` (or `host provision`).",
            file=sys.stderr,
        )
    return 0
