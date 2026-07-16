"""Restic backup/restore for Compose named volumes (django_media) via docker run restic/restic."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Final, Literal

from catalpa_tooling.cli_confirm import confirm_by_typing_env_name
from catalpa_tooling.config import DEFAULT_RESTIC_DATA_VOLUME, ProjectConfig, ProjectConfigError
from catalpa_tooling.run_cmd import run as run_cmd

# Like PGBR_S3_WRITE_* / PGBR_S3_READ_*: use one credential source per environment (mutually exclusive).
RESTIC_WRITE_PREFIX: Final[str] = "RESTIC_WRITE_"
RESTIC_READ_PREFIX: Final[str] = "RESTIC_READ_"

# Suffix after RESTIC_WRITE_ / RESTIC_READ_ → canonical env key for restic / docker -e.
_RESTIC_PREFIX_SUFFIX_TO_CANONICAL: tuple[tuple[str, str], ...] = (
    ("REPOSITORY", "RESTIC_REPOSITORY"),
    ("PASSWORD", "RESTIC_PASSWORD"),
    ("S3_ACCESS_KEY_ID", "RESTIC_S3_ACCESS_KEY_ID"),
    ("S3_SECRET_ACCESS_KEY", "RESTIC_S3_SECRET_ACCESS_KEY"),
    ("S3_DEFAULT_REGION", "RESTIC_S3_DEFAULT_REGION"),
    ("S3_SESSION_TOKEN", "RESTIC_S3_SESSION_TOKEN"),
)

# Subcommands allowed when only RESTIC_READ_* is set (snapshots/check/stats/restore and read-only helpers).
_RESTIC_READ_OK_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "snapshots",
        "check",
        "stats",
        "restore",
        "find",
        "list",
        "ls",
        "cat",
        "diff",
        "version",
        "help",
    }
)

# Official image; pin patch release (Docker Hub has 0.17.x, not bare 0.17).
RESTIC_IMAGE: Final[str] = "restic/restic:0.17.3"

# Default mount when ``ops.restic.data_volume`` is ``django_media`` (must match paths in snapshots).
DJANGO_MEDIA_MOUNT: Final[str] = "/backup/django_media"

# Env keys written to restic-files-backup.env for systemd (see restic-files-backup.sh).
RESTIC_FILES_DATA_VOLUME_KEY: Final[str] = "RESTIC_FILES_DATA_VOLUME"
RESTIC_FILES_BACKUP_PATH_KEY: Final[str] = "RESTIC_FILES_BACKUP_PATH"


def restic_data_volume_key(config: ProjectConfig | None) -> str:
    """Compose volume key from ``ops.restic.data_volume`` (default ``django_media``)."""
    if config is not None:
        return config.ops.restic.data_volume
    return DEFAULT_RESTIC_DATA_VOLUME


def _restic_data_volume_key(config: ProjectConfig | None) -> str:
    return restic_data_volume_key(config)


def restic_backup_mount_path(*, config: ProjectConfig | None = None) -> str:
    """Path inside the restic container used for backup/restore snapshot paths.

    Defaults to ``/backup/<ops.restic.data_volume>``; override with ``ops.restic.backup_path``
    when existing snapshots use a different prefix (legacy host mounts).
    """
    if config is not None and config.ops.restic.backup_path:
        return config.ops.restic.backup_path
    return f"/backup/{_restic_data_volume_key(config)}"

def _default_compose_project(config: ProjectConfig | None) -> str:
    if config is None:
        raise ProjectConfigError(
            "ProjectConfig is required when COMPOSE_PROJECT_NAME is unset"
        )
    return config.stack.compose_project_default


def _sanitize_dk_env_for_compose_project_suffix(dk_env_name: str) -> str:
    """Lowercase slug safe for Docker Compose project names (from ``dk <env>`` folder name)."""
    s = (dk_env_name or "").strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "-", s)
    s = s.strip("-") or "env"
    return s[:48]


def _docker_host_targets_local_engine(docker_host: str) -> bool:
    """True when ``DOCKER_HOST`` targets the local Docker engine (not SSH to a remote daemon)."""
    s = (docker_host or "").strip()
    if not s:
        return True
    low = s.lower()
    if low.startswith("ssh://"):
        return False
    if low.startswith("unix://") or low.startswith("npipe://"):
        return True
    if low.startswith("tcp://127.0.0.1") or low.startswith("tcp://localhost"):
        return True
    return False


def django_media_volume_name(
    compose_project_name: str,
    *,
    config: ProjectConfig | None = None,
) -> str:
    """Docker volume name for the restic backup target (``{project}_{ops.restic.data_volume}``)."""
    project = (compose_project_name or "").strip()
    if not project:
        project = _default_compose_project(config)
    vol_key = _restic_data_volume_key(config)
    return f"{project}_{vol_key}"


def staging_volume_name(
    compose_project_name: str,
    *,
    config: ProjectConfig | None = None,
) -> str:
    """Ephemeral named volume for restic restore extract (same project prefix)."""
    project = (compose_project_name or "").strip()
    if not project:
        project = _default_compose_project(config)
    return f"{project}_restic_restore_staging"


def _has_nonempty_prefix_keys(env: dict[str, str], prefix: str) -> bool:
    for k, v in env.items():
        if k.startswith(prefix) and str(v).strip():
            return True
    return False


def _has_legacy_restic_credentials(env: dict[str, str]) -> bool:
    """True if legacy flat ``RESTIC_REPOSITORY`` / ``RESTIC_PASSWORD`` / ``RESTIC_S3_*`` are set."""
    if (env.get("RESTIC_REPOSITORY") or "").strip():
        return True
    if (env.get("RESTIC_PASSWORD") or "").strip():
        return True
    for k, v in env.items():
        if k.startswith("RESTIC_S3_") and str(v).strip():
            return True
    return False


def restic_credentials_conflict_message(env: dict[str, str]) -> str | None:
    """If legacy ``RESTIC_*`` mixes with ``RESTIC_WRITE_*`` or ``RESTIC_READ_*``, return an error."""
    leg = _has_legacy_restic_credentials(env)
    w = _has_nonempty_prefix_keys(env, RESTIC_WRITE_PREFIX)
    r = _has_nonempty_prefix_keys(env, RESTIC_READ_PREFIX)
    if int(leg) + int(w) + int(r) <= 1:
        return None
    return (
        "restic: RESTIC_WRITE_*, RESTIC_READ_*, and legacy RESTIC_REPOSITORY / RESTIC_PASSWORD / RESTIC_S3_* "
        "are mutually exclusive.\n"
        f"  legacy={leg} RESTIC_WRITE_*={w} RESTIC_READ_*={r}"
    )


def restic_credential_mode(env: dict[str, str]) -> Literal["write", "read"] | None:
    """Which credential block is active, or None if no restic repo vars are set."""
    if restic_credentials_conflict_message(env):
        return None
    if _has_nonempty_prefix_keys(env, RESTIC_WRITE_PREFIX):
        return "write"
    if _has_legacy_restic_credentials(env):
        return "write"
    if _has_nonempty_prefix_keys(env, RESTIC_READ_PREFIX):
        return "read"
    return None


def normalize_restic_credentials(env: dict[str, str]) -> dict[str, str]:
    """Copy ``env`` and overlay canonical ``RESTIC_*`` keys from ``RESTIC_WRITE_*`` / ``RESTIC_READ_*`` / legacy."""
    out = dict(env)
    mode = restic_credential_mode(env)
    if mode == "write" and _has_nonempty_prefix_keys(env, RESTIC_WRITE_PREFIX):
        for suffix, canonical in _RESTIC_PREFIX_SUFFIX_TO_CANONICAL:
            src = f"{RESTIC_WRITE_PREFIX}{suffix}"
            if (out.get(src) or "").strip():
                out[canonical] = str(out[src]).strip()
    elif mode == "read":
        for suffix, canonical in _RESTIC_PREFIX_SUFFIX_TO_CANONICAL:
            src = f"{RESTIC_READ_PREFIX}{suffix}"
            if (out.get(src) or "").strip():
                out[canonical] = str(out[src]).strip()
    return out


def _read_mode_argv_error(restic_argv: list[str]) -> str | None:
    if not restic_argv:
        return "restic: missing subcommand (RESTIC_READ_* allows read-only operations only)"
    sub = restic_argv[0]
    if sub not in _RESTIC_READ_OK_SUBCOMMANDS:
        return (
            f"restic: subcommand {sub!r} is not allowed with RESTIC_READ_* credentials "
            "(snapshots, check, stats, restore, …; use RESTIC_WRITE_* or legacy RESTIC_* for init/backup and other "
            "repo changes)."
        )
    return None


def validate_restic_env(env: dict[str, str]) -> str | None:
    """Return an error message if required keys are missing (after prefix resolution), else None."""
    err = restic_credentials_conflict_message(env)
    if err:
        return err
    n = normalize_restic_credentials(dict(env))
    if not (n.get("RESTIC_REPOSITORY") or "").strip():
        return (
            "RESTIC_REPOSITORY is required "
            "(set RESTIC_WRITE_REPOSITORY, RESTIC_READ_REPOSITORY, or legacy RESTIC_REPOSITORY)"
        )
    if not (n.get("RESTIC_PASSWORD") or "").strip():
        return (
            "RESTIC_PASSWORD is required "
            "(set RESTIC_WRITE_PASSWORD, RESTIC_READ_PASSWORD, or legacy RESTIC_PASSWORD)"
        )
    return None


def validate_restic_env_for_systemd(env: dict[str, str]) -> str | None:
    """Scheduled backups and ``install-systemd`` require write credentials, not ``RESTIC_READ_*`` only."""
    err = validate_restic_env(env)
    if err:
        return err
    if restic_credential_mode(env) == "read":
        return (
            "RESTIC_READ_* cannot be used for systemd backup units or scheduled backups; "
            "use RESTIC_WRITE_* or legacy RESTIC_REPOSITORY / RESTIC_PASSWORD / RESTIC_S3_* on the backup host."
        )
    return None


def split_restic_cli_verbose(argv: list[str]) -> tuple[list[str], int]:
    """Remove ``-v`` / ``--verbose`` tokens; return ``(argv_without_flags, count)`` for restic ``-v`` flags."""
    vcount = 0
    out: list[str] = []
    for a in argv:
        if a == "-v":
            vcount += 1
        elif a == "--verbose":
            vcount += 1
        else:
            out.append(a)
    return out, vcount


def maybe_warn_restic_repository_url(repo: str) -> None:
    """If ``RESTIC_REPOSITORY`` looks like a bare HTTP URL, hint about ``s3:`` and other backends."""
    r = (repo or "").strip()
    if r.startswith("https://") or r.startswith("http://"):
        print(
            "Hint: RESTIC_REPOSITORY looks like an HTTP(S) URL. restic needs a backend prefix "
            "(e.g. `s3:region.digitaloceanspaces.com/bucket-name` for DigitalOcean Spaces, "
            "not `https://…`). See restic S3 backend documentation.",
            file=sys.stderr,
        )


def _merged_process_env(env: dict[str, str]) -> dict[str, str]:
    """Merge into the current process env for `docker run` (DOCKER_HOST, RESTIC_*, …)."""
    out = os.environ.copy()
    out.update(env)
    return out


# Restic's S3 backend reads AWS_*; we use RESTIC_S3_* on the host for any S3-compatible provider.
_RESTIC_S3_TO_AWS: tuple[tuple[str, str], ...] = (
    ("RESTIC_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID"),
    ("RESTIC_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY"),
    ("RESTIC_S3_DEFAULT_REGION", "AWS_DEFAULT_REGION"),
    ("RESTIC_S3_SESSION_TOKEN", "AWS_SESSION_TOKEN"),
)


def _apply_restic_s3_credentials(out: dict[str, str]) -> None:
    """If RESTIC_S3_* are set, copy them into AWS_* for the restic process (overrides same-named AWS_*)."""
    for src, dst in _RESTIC_S3_TO_AWS:
        v = (out.get(src) or "").strip()
        if v:
            out[dst] = v


def aws_env_vars_for_s3_restic_env_file(n: dict[str, str]) -> dict[str, str]:
    """``AWS_*`` keys for systemd / ``docker --env-file`` when the repo is S3-compatible.

    restic's S3 backend reads ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` / region; deploy env
    files may store only ``RESTIC_S3_*``. The backup shell maps those at runtime; env files need the
    duplicate ``AWS_*`` lines for ``docker run --env-file`` (e.g. Zabbix ``restic.snapshots``).
    """
    tmp = dict(n)
    _apply_restic_s3_credentials(tmp)
    out: dict[str, str] = {}
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION", "AWS_SESSION_TOKEN"):
        v = (tmp.get(key) or "").strip()
        if v:
            out[key] = v
    return out


def _merged_process_env_restic(env: dict[str, str]) -> dict[str, str]:
    """Like `_merged_process_env`, plus RESTIC_WRITE_/READ_/legacy resolution and RESTIC_S3_* → AWS_*."""
    out = _merged_process_env(normalize_restic_credentials(env))
    _apply_restic_s3_credentials(out)
    return out


# Env vars the restic binary reads inside the container (must be passed with ``docker run -e``;
# the docker CLI does not inject the subprocess environment into the container by default).
_RESTIC_CONTAINER_ENV_KEYS: tuple[str, ...] = (
    "RESTIC_REPOSITORY",
    "RESTIC_PASSWORD",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_DEFAULT_REGION",
    "AWS_SESSION_TOKEN",
)


def _docker_run_env_flags(merged: dict[str, str]) -> list[str]:
    """``docker run -e KEY`` for each key that is set (values come from ``merged`` via subprocess env)."""
    out: list[str] = []
    for key in _RESTIC_CONTAINER_ENV_KEYS:
        if (merged.get(key) or "").strip():
            out.extend(["-e", key])
    return out


def _restic_image(merged: dict[str, str]) -> str:
    return (merged.get("RESTIC_IMAGE") or "").strip() or RESTIC_IMAGE


def _restic_verbose_flags(merged: dict[str, str]) -> list[str]:
    """Map ``RESTIC_VERBOSE`` (1–4 or true/yes) to repeated ``-v`` (restic global verbosity)."""
    raw = (merged.get("RESTIC_VERBOSE") or "").strip()
    if not raw:
        return []
    if raw.isdigit():
        n = max(0, min(int(raw), 4))
        return ["-v"] * n
    if raw.lower() in ("true", "yes", "on"):
        return ["-v"]
    return []


def merge_restic_verbose_from_cli(env: dict[str, str], cli_vcount: int) -> dict[str, str]:
    """Add CLI ``-v`` count to optional ``RESTIC_VERBOSE`` from credentials (cap total at 4)."""
    if cli_vcount <= 0:
        return env
    out = dict(env)
    ev = (out.get("RESTIC_VERBOSE") or "").strip()
    env_n = int(ev) if ev.isdigit() else (1 if ev.lower() in ("true", "yes", "on") else 0)
    out["RESTIC_VERBOSE"] = str(min(4, cli_vcount + env_n))
    return out


def restic_env_for_restore(env: dict[str, str]) -> dict[str, str]:
    """Verbosity for ``bkp_files restore`` (progress during long extracts).

    ``RESTIC_RESTORE_VERBOSE`` / ``restic_restore_verbose`` overrides ``RESTIC_VERBOSE`` for restore only.
    When neither is set, defaults to ``1`` (one ``-v``), similar to pgBackRest restore's default
    ``--log-level-console=info``. Set ``restic_restore_verbose: 0`` for a quiet restore.
    """
    out = dict(env)
    restore_v = (out.get("RESTIC_RESTORE_VERBOSE") or "").strip()
    if restore_v:
        out["RESTIC_VERBOSE"] = restore_v
    elif not (out.get("RESTIC_VERBOSE") or "").strip():
        out["RESTIC_VERBOSE"] = "1"
    return out


def _docker_run_restic(
    env: dict[str, str],
    volume_mounts: list[tuple[str, str, bool]],
    restic_argv: list[str],
) -> int:
    """Run `docker run --rm` with restic image (entrypoint is `restic`)."""
    mode = restic_credential_mode(env)
    if mode == "read":
        rerr = _read_mode_argv_error(restic_argv)
        if rerr:
            print(rerr, file=sys.stderr)
            return 1
        # Read-only IAM policies deny s3:PutObject; restic still tries to write a repo lock. Use the global
        # --no-lock flag so snapshots/restore/check/etc. work against read-only credentials.
        restic_argv = ["--no-lock", *restic_argv]
    merged = _merged_process_env_restic(env)
    maybe_warn_restic_repository_url(merged.get("RESTIC_REPOSITORY") or "")
    cmd: list[str] = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
    ]
    from catalpa_tooling.dc_backup.hosts import (
        docker_add_host_args,
        docker_ca_env_flags_for_restic,
        docker_ca_volume_args,
    )

    cmd.extend(docker_add_host_args(merged))
    cmd.extend(docker_ca_volume_args(merged))
    cmd.extend(_docker_run_env_flags(merged))
    cmd.extend(docker_ca_env_flags_for_restic(merged))
    for src, dest, ro in volume_mounts:
        spec = f"{src}:{dest}"
        if ro:
            spec += ":ro"
        cmd.extend(["-v", spec])
    cmd.append(_restic_image(merged))
    cmd.extend(_restic_verbose_flags(merged))
    cmd.extend(restic_argv)
    r = run_cmd(cmd, env=merged, check=False)
    return r.returncode


def _docker_run_restic_repo_only(env: dict[str, str], restic_argv: list[str]) -> int:
    """Run restic subcommands that only need repository credentials (no volume)."""
    return _docker_run_restic(env, [], restic_argv)


def _docker_run_alpine(
    env: dict[str, str],
    volume_mounts: list[tuple[str, str, bool]],
    shell_cmd: str,
) -> int:
    """Run alpine for copy/rm helpers (restore promotion)."""
    cmd: list[str] = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
    ]
    for src, dest, ro in volume_mounts:
        spec = f"{src}:{dest}"
        if ro:
            spec += ":ro"
        cmd.extend(["-v", spec])
    cmd.extend(["alpine:3.21", "sh", "-c", shell_cmd])
    r = run_cmd(cmd, env=_merged_process_env_restic(env), check=False)
    return r.returncode


def run_restic_command(
    env: dict[str, str],
    restic_argv: list[str],
    *,
    read_only_django_media: bool,
    config: ProjectConfig | None = None,
) -> int:
    """Run `restic …` with the configured compose volume mounted at ``restic_backup_mount_path``."""
    err = validate_restic_env(env)
    if err:
        print(err, file=sys.stderr)
        return 1
    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip()
    mount = restic_backup_mount_path(config=config)
    vol = django_media_volume_name(project, config=config)
    return _docker_run_restic(
        env,
        [(vol, mount, read_only_django_media)],
        restic_argv,
    )


def run_backup(env: dict[str, str], *, config: ProjectConfig | None = None) -> int:
    """restic backup of the configured compose volume."""
    mount = restic_backup_mount_path(config=config)
    return run_restic_command(
        env,
        ["backup", mount],
        read_only_django_media=True,
        config=config,
    )


def run_snapshots(env: dict[str, str]) -> int:
    err = validate_restic_env(env)
    if err:
        print(err, file=sys.stderr)
        return 1
    return _docker_run_restic_repo_only(env, ["snapshots"])


def run_init(env: dict[str, str]) -> int:
    """Create a new restic repository at RESTIC_REPOSITORY (run once per empty repo)."""
    err = validate_restic_env(env)
    if err:
        print(err, file=sys.stderr)
        return 1
    return _docker_run_restic_repo_only(env, ["init"])


def run_check(env: dict[str, str]) -> int:
    err = validate_restic_env(env)
    if err:
        print(err, file=sys.stderr)
        return 1
    return _docker_run_restic_repo_only(env, ["check"])


def run_stats(env: dict[str, str]) -> int:
    err = validate_restic_env(env)
    if err:
        print(err, file=sys.stderr)
        return 1
    return _docker_run_restic_repo_only(env, ["stats"])


def run_restore(
    env: dict[str, str],
    snapshot: str,
    *,
    env_name: str,
    skip_confirm: bool,
    config: ProjectConfig | None = None,
) -> int:
    """Restore snapshot into django_media: extract to a staging volume, then sync into the live volume.

    Destructive: clears current contents of django_media (including dotfiles) before copy-back.
    """
    err = validate_restic_env(env)
    if err:
        print(err, file=sys.stderr)
        return 1
    env = restic_env_for_restore(env)
    project = (env.get("COMPOSE_PROJECT_NAME") or "").strip()
    mount = restic_backup_mount_path(config=config)
    vol_key = _restic_data_volume_key(config)
    media_vol = django_media_volume_name(project, config=config)
    stage_vol = staging_volume_name(project, config=config)

    if not skip_confirm:
        print(
            f"WARNING: This will replace all files in the {vol_key!r} volume "
            f"({media_vol}) with snapshot {snapshot!r}.",
            file=sys.stderr,
        )
        print(f"  Environment: {env_name}", file=sys.stderr)
        if not confirm_by_typing_env_name(env_name):
            print("Cancelled.", file=sys.stderr)
            return 1

    run_cmd(
        ["docker", "volume", "rm", "-f", stage_vol],
        env=_merged_process_env_restic(env),
        capture_output=True,
        check=False,
    )
    c1 = run_cmd(
        ["docker", "volume", "create", stage_vol],
        env=_merged_process_env_restic(env),
        capture_output=True,
        text=True,
        check=False,
    )
    if c1.returncode != 0:
        print(c1.stderr or c1.stdout or "docker volume create failed", file=sys.stderr)
        return 1

    try:
        # Extract snapshot path into staging; snapshot layout uses the backup mount prefix.
        rc = _docker_run_restic(
            env,
            [
                (stage_vol, "/restore", False),
                (media_vol, mount, True),
            ],
            [
                "restore",
                snapshot,
                "--target",
                "/restore",
                "--path",
                mount,
            ],
        )
        if rc != 0:
            return rc

        # Promote: restored tree is /restore{mount}/... (leading segment = mount path).
        inner = "/stage" + mount
        script = (
            f"set -e; rm -rf /live/* /live/.[!.]* /live/..?* 2>/dev/null || true; "
            f"if [ -d {inner} ]; then cp -a {inner}/. /live/; else "
            f"echo 'Expected tree not found at {inner}' >&2; exit 1; fi"
        )
        rc = _docker_run_alpine(
            env,
            [
                (media_vol, "/live", False),
                (stage_vol, "/stage", False),
            ],
            script,
        )
        return rc
    finally:
        run_cmd(
            ["docker", "volume", "rm", "-f", stage_vol],
            env=_merged_process_env_restic(env),
            capture_output=True,
            check=False,
        )


def load_dotenv(repo_root: Path | str | None = None) -> None:
    """Load repo-root .env into os.environ (non-destructive for existing keys)."""
    try:
        from dotenv import load_dotenv as _load_dotenv
    except ImportError:
        return
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    _load_dotenv(root / ".env", override=False)


def compose_project_name_from_compose_config(compose_file: str, env: dict[str, str]) -> str | None:
    """Return Compose JSON ``name`` from ``docker compose config`` (diagnostics; not used for volume prefixes).

    ``compose.yml`` external volume names use ``${COMPOSE_PROJECT_NAME:-<stack.compose_project_default>}_…``. The JSON
    project ``name`` often follows the checkout directory when ``COMPOSE_PROJECT_NAME`` is unset,
    which can differ from that volume prefix—do not treat it as ``COMPOSE_PROJECT_NAME``.
    """
    r = run_cmd(
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "config",
            "--format",
            "json",
        ],
        env=_merged_process_env_restic(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    name = data.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def resolve_env_with_compose_project(
    compose_file: str,
    base_env: dict[str, str],
    *,
    dk_env_name: str = "",
    config: ProjectConfig | None = None,
) -> dict[str, str]:
    """Set ``COMPOSE_PROJECT_NAME`` when missing so stacks and named volumes stay consistent."""
    _ = compose_file
    out = dict(base_env)
    if (out.get("COMPOSE_PROJECT_NAME") or "").strip():
        return out

    default = _default_compose_project(config)
    docker_host = (out.get("DOCKER_HOST") or "").strip()
    if _docker_host_targets_local_engine(docker_host):
        suffix = _sanitize_dk_env_for_compose_project_suffix(dk_env_name)
        out["COMPOSE_PROJECT_NAME"] = f"{default}_{suffix}"
    else:
        out["COMPOSE_PROJECT_NAME"] = default
    return out
