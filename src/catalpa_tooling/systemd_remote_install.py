"""Install pgBackRest + restic systemd units on a deploy host via SSH (see README_SYSTEMD.md)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.cli_confirm import confirm_by_typing_env_name
from catalpa_tooling.restic_files import (
    RESTIC_FILES_BACKUP_PATH_KEY,
    RESTIC_FILES_DATA_VOLUME_KEY,
    aws_env_vars_for_s3_restic_env_file,
    normalize_restic_credentials,
    resolve_env_with_compose_project,
    restic_backup_mount_path,
    validate_restic_env_for_systemd,
    restic_data_volume_key,
)
from catalpa_tooling.systemd_assets import systemd_source_dir
from catalpa_tooling.systemd_render import render_systemd_unit

REMOTE_SYSTEMD = "/etc/systemd/system"

SCRIPTS = (
    ("pgbackrest-backup.sh", 0o755),
    ("restic-files-backup.sh", 0o755),
)


def parse_docker_host_to_ssh_target(docker_host: str) -> str:
    """Return ``user@host`` for OpenSSH from ``docker_host`` (e.g. ``ssh://root@host``)."""
    raw = (docker_host or "").strip()
    if not raw:
        raise ValueError("docker_host is empty; set it in docker/envs/<env>/info.yaml")

    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme and parsed.scheme != "ssh":
            raise ValueError(f"Unsupported docker_host scheme {parsed.scheme!r}; expected ssh://")
        netloc = parsed.netloc or ""
        if not netloc and parsed.path:
            # ssh://user@host sometimes parses path
            netloc = parsed.path.lstrip("/")
        if not netloc:
            raise ValueError(f"Could not parse host from docker_host: {raw!r}")
        if "@" not in netloc:
            raise ValueError(
                f"docker_host {raw!r} has no SSH user; use ssh://root@host or user@host"
            )
        return netloc

    # user@host without scheme
    if "@" in raw:
        return raw
    raise ValueError(
        f"docker_host {raw!r} has no user; use ssh://root@host or user@host"
    )


def _secretish_key(key: str) -> bool:
    u = key.upper()
    return any(
        x in u
        for x in (
            "PASSWORD",
            "SECRET",
            "TOKEN",
            "PRIVATE",
            "ACCESS_KEY",
            "SECRET_ACCESS",
        )
    )


def redact_env_file_content(content: str) -> str:
    """Redact values for lines whose keys look sensitive (dry-run output)."""
    out_lines: list[str] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            out_lines.append(line)
            continue
        if "=" not in line:
            out_lines.append(line)
            continue
        key, _, _val = line.partition("=")
        key = key.strip()
        if _secretish_key(key):
            out_lines.append(f"{key}=<redacted>")
        else:
            out_lines.append(line)
    return "\n".join(out_lines) + ("\n" if content.endswith("\n") else "")


def parse_install_systemd_flags(
    argv: list[str],
    *,
    fixed_only: str | None = None,
) -> tuple[bool, bool, str | None]:
    """Parse ``--dry-run``, ``--enable``, and optionally ``--only pgbackrest|restic`` from args.

    When ``fixed_only`` is ``\"pgbackrest\"`` or ``\"restic\"``, that component is implied
    and ``--only`` must not appear.
    """
    dry_run = False
    enable = False
    only: str | None = fixed_only
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dry-run":
            dry_run = True
            i += 1
        elif a == "--enable":
            enable = True
            i += 1
        elif a == "--only":
            if fixed_only is not None:
                raise ValueError(
                    "--only is not valid for this command; it installs a single component only."
                )
            if i + 1 >= len(argv):
                raise ValueError("--only requires pgbackrest or restic")
            val = argv[i + 1].lower()
            if val not in ("pgbackrest", "restic"):
                raise ValueError("--only must be pgbackrest or restic")
            only = val
            i += 2
        else:
            raise ValueError(f"Unknown argument: {a!r}")
    return dry_run, enable, only


def render_pgbackrest_env(env: dict[str, str], *, project_name: str = "project") -> str:
    """Key=value lines for ``<config_dir>/pgbackrest-backup.env``."""
    stanza = (env.get("PGBR_STANZA") or env.get("PGBR_S3_WRITE_STANZA") or "").strip()
    container = (env.get("PGBR_DB_CONTAINER") or "").strip()
    lines = [
        f"# Managed by {project_name} deploy (bkp_db install-systemd).",
        f"PGBR_DB_CONTAINER={container}",
        f"PGBR_STANZA={stanza}",
    ]
    for key in ("PGBR_LOG_LEVEL_CONSOLE", "PGBR_LOG_LEVEL_STDERR"):
        v = (env.get(key) or "").strip()
        if v:
            lines.append(f"{key}={v}")
    return "\n".join(lines) + "\n"


def render_restic_env(
    env: dict[str, str],
    *,
    project_name: str = "project",
    config: ProjectConfig | None = None,
) -> str:
    """Key=value lines for ``<config_dir>/restic-files-backup.env`` (canonical ``RESTIC_*``)."""
    n = normalize_restic_credentials(dict(env))
    lines = [
        f"# Managed by {project_name} deploy (bkp_files install-systemd).",
    ]
    project = (n.get("COMPOSE_PROJECT_NAME") or "").strip()
    if project:
        lines.append(f"COMPOSE_PROJECT_NAME={project}")
    lines.append(f"{RESTIC_FILES_DATA_VOLUME_KEY}={restic_data_volume_key(config)}")
    backup_path = restic_backup_mount_path(config=config)
    default_backup_path = f"/backup/{restic_data_volume_key(config)}"
    if backup_path != default_backup_path:
        lines.append(f"{RESTIC_FILES_BACKUP_PATH_KEY}={backup_path}")
    order = [
        "RESTIC_REPOSITORY",
        "RESTIC_PASSWORD",
        "RESTIC_IMAGE",
    ]
    for key in order:
        v = (n.get(key) or "").strip()
        if v:
            lines.append(f"{key}={v}")
    for key in sorted(k for k in n if k.startswith("RESTIC_S3_")):
        v = (n.get(key) or "").strip()
        if v:
            lines.append(f"{key}={v}")
    for ak, av in sorted(aws_env_vars_for_s3_restic_env_file(n).items()):
        lines.append(f"{ak}={av}")
    return "\n".join(lines) + "\n"


def discover_db_container(ssh_target: str, compose_project: str) -> str | None:
    """Pick a single running Postgres container name on the remote host, or None."""
    r = run_cmd(
        ["ssh", "-o", "BatchMode=yes", ssh_target, "docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
        print_cmd=True,
    )
    if r.returncode != 0:
        print(r.stderr or r.stdout or "ssh docker ps failed", file=sys.stderr)
        return None
    names = [n.strip() for n in r.stdout.splitlines() if n.strip()]
    if not names:
        return None
    project = (compose_project or "").strip()
    # Compose common patterns: project_db_1, project-db-1
    def is_db(n: str) -> bool:
        nl = n.lower()
        return "_db_" in nl or "-db-" in nl or nl.endswith("_db_1")

    candidates = [n for n in names if is_db(n)]
    if not candidates:
        return None
    if project:
        pref = [n for n in candidates if n == f"{project}_db_1" or n.startswith(f"{project}_") or project in n]
        if len(pref) == 1:
            return pref[0]
        if len(pref) > 1:
            return None
    if len(candidates) == 1:
        return candidates[0]
    return None


def _ssh_run(ssh_target: str, remote_cmd: str, *, check: bool = True) -> int:
    r = run_cmd(
        ["ssh", "-o", "BatchMode=yes", ssh_target, remote_cmd],
        check=False,
    )
    if check and r.returncode != 0:
        return r.returncode
    return r.returncode


def _scp_push(ssh_target: str, local_paths: list[Path], remote_dir: str) -> int:
    if not local_paths:
        return 0
    cmd = ["scp", "-q", "-o", "BatchMode=yes", *[str(p) for p in local_paths], f"{ssh_target}:{remote_dir}"]
    r = run_cmd(cmd, check=False)
    return r.returncode


def cmd_install_systemd_backups(
    config: ProjectConfig,
    env_add: dict[str, str],
    docker_host: str,
    env_name: str,
    argv: list[str],
    *,
    global_dry_run: bool,
    yes: bool,
    fixed_only: str | None = None,
) -> int:
    """Install systemd backup units on the host implied by ``docker_host``."""
    try:
        dry_run, enable, only = parse_install_systemd_flags(argv, fixed_only=fixed_only)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        if fixed_only == "pgbackrest":
            usage = "bkp_db install-systemd [--dry-run] [--enable]"
        elif fixed_only == "restic":
            usage = "bkp_files install-systemd [--dry-run] [--enable]"
        else:
            usage = "[--dry-run] [--enable] [--only pgbackrest|restic]"
        print(f"usage: {usage}", file=sys.stderr)
        return 1

    if global_dry_run:
        dry_run = True

    try:
        ssh_target = parse_docker_host_to_ssh_target(docker_host)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    env_r = resolve_env_with_compose_project(
        config.compose_prod, dict(env_add), dk_env_name=env_name, config=config
    )
    project = (env_r.get("COMPOSE_PROJECT_NAME") or "").strip()

    do_pgbr = only is None or only == "pgbackrest"
    do_restic = only is None or only == "restic"

    stanza = (env_r.get("PGBR_S3_WRITE_STANZA") or env_r.get("PGBR_STANZA") or "").strip()
    if do_pgbr:
        env_r["PGBR_STANZA"] = stanza

    if do_pgbr and not stanza:
        print(
            "Skipping pgBackRest systemd files: PGBR_S3_WRITE_STANZA is not set (WRITE mode / stanza required).",
            file=sys.stderr,
        )
        do_pgbr = False

    restic_err = validate_restic_env_for_systemd(env_r) if do_restic else None
    if do_restic and restic_err:
        print(f"Skipping restic systemd files: {restic_err}", file=sys.stderr)
        do_restic = False

    if not do_pgbr and not do_restic:
        print("Nothing to install (pgBackRest and restic both skipped).", file=sys.stderr)
        return 1

    container = (env_r.get("PGBR_DB_CONTAINER") or "").strip()
    if do_pgbr and not container and not dry_run:
        discovered = discover_db_container(ssh_target, project)
        if discovered:
            container = discovered
            env_r["PGBR_DB_CONTAINER"] = discovered
            print(f"PGBR_DB_CONTAINER={discovered} (discovered via docker ps on remote)", file=sys.stderr)
        else:
            print(
                "Could not discover a unique db container on the remote host. "
                "Set pgbr_db_container in credentials (maps to PGBR_DB_CONTAINER) or ensure exactly one matching container is running.",
                file=sys.stderr,
            )
            return 1
    elif do_pgbr and not container and dry_run:
        print(
            "(dry-run) Would discover PGBR_DB_CONTAINER via ssh docker ps or use pgbr_db_container from credentials.",
            file=sys.stderr,
        )
        env_r["PGBR_DB_CONTAINER"] = env_r.get("PGBR_DB_CONTAINER") or config.ops.default_db_container

    project_name = config.meta.name
    pgbr_body = render_pgbackrest_env(env_r, project_name=project_name) if do_pgbr else ""
    restic_body = render_restic_env(env_r, project_name=project_name, config=config) if do_restic else ""

    if enable and not yes and not sys.stdin.isatty():
        print(
            "Refusing --enable without a TTY. Pass --yes for non-interactive use.",
            file=sys.stderr,
        )
        return 1

    install_prefix = config.ops.install_prefix
    config_dir = config.ops.config_dir
    units_pgbr = config.ops.systemd_units.pgbackrest
    units_restic = config.ops.systemd_units.restic
    timers_pgbr = config.ops.systemd_units.timers_enable_pgbackrest
    timers_restic = config.ops.systemd_units.timers_enable_restic
    systemd_src = systemd_source_dir()
    if not systemd_src.is_dir():
        print(f"Missing bundled systemd assets: {systemd_src}", file=sys.stderr)
        return 1

    print(f"SSH target: {ssh_target}", file=sys.stderr)
    print(f"Would install: pgBackRest={do_pgbr} restic={do_restic} dry_run={dry_run} enable={enable}", file=sys.stderr)

    unit_names: list[str] = []
    if do_pgbr:
        unit_names.extend(units_pgbr)
    if do_restic:
        unit_names.extend(units_restic)

    if dry_run:
        if unit_names:
            print(f"(dry-run) Would install units: {' '.join(unit_names)}", file=sys.stderr)
        if do_pgbr:
            print(f"--- {config_dir}/pgbackrest-backup.env (redacted) ---", file=sys.stderr)
            print(redact_env_file_content(pgbr_body), end="", file=sys.stderr)
        if do_restic:
            print(f"--- {config_dir}/restic-files-backup.env (redacted) ---", file=sys.stderr)
            print(redact_env_file_content(restic_body), end="", file=sys.stderr)
        print("(dry-run) No files copied; no remote commands run.", file=sys.stderr)
        return 0

    if enable and sys.stdin.isatty() and not yes:
        print(
            "About to enable --now systemd timer(s) on the remote host.",
            file=sys.stderr,
        )
        print(f"  Environment: {env_name}", file=sys.stderr)
        if not confirm_by_typing_env_name(env_name):
            print("Cancelled.", file=sys.stderr)
            return 1

    rc = _ssh_run(
        ssh_target,
        f"mkdir -p {install_prefix} {config_dir} {REMOTE_SYSTEMD}",
    )
    if rc != 0:
        return rc

    to_scp_opt: list[Path] = []
    if do_pgbr:
        to_scp_opt.append(systemd_src / "pgbackrest-backup.sh")
    if do_restic:
        to_scp_opt.append(systemd_src / "restic-files-backup.sh")
    for p in to_scp_opt:
        if not p.is_file():
            print(f"Missing {p}", file=sys.stderr)
            return 1

    rc = _scp_push(ssh_target, to_scp_opt, install_prefix)
    if rc != 0:
        return rc

    mode_map: list[tuple[str, str]] = []
    for name, mode in SCRIPTS:
        if (do_pgbr and name == "pgbackrest-backup.sh") or (do_restic and name == "restic-files-backup.sh"):
            mode_map.append((f"{install_prefix}/{name}", format(mode, "o")))

    for remote, mode in mode_map:
        rc = _ssh_run(ssh_target, f"chmod {mode} {remote}")
        if rc != 0:
            return rc

    with tempfile.TemporaryDirectory() as tmp:
        tdir = Path(tmp)
        to_scp_units: list[Path] = []
        for name in unit_names:
            try:
                body = render_systemd_unit(
                    name,
                    install_prefix=install_prefix,
                    config_dir=config_dir,
                )
            except (ValueError, FileNotFoundError) as e:
                print(str(e), file=sys.stderr)
                return 1
            local_unit = tdir / name
            local_unit.write_text(body, encoding="utf-8")
            to_scp_units.append(local_unit)

        if to_scp_units:
            rc = _scp_push(ssh_target, to_scp_units, REMOTE_SYSTEMD)
            if rc != 0:
                return rc

        env_files: list[tuple[Path, str]] = []
        if do_pgbr:
            ep = tdir / "pgbackrest-backup.env"
            ep.write_text(pgbr_body, encoding="utf-8")
            env_files.append((ep, f"{config_dir}/pgbackrest-backup.env"))
        if do_restic:
            er = tdir / "restic-files-backup.env"
            er.write_text(restic_body, encoding="utf-8")
            env_files.append((er, f"{config_dir}/restic-files-backup.env"))

        for local_path, remote_path in env_files:
            rc = run_cmd(
                ["scp", "-q", "-o", "BatchMode=yes", str(local_path), f"{ssh_target}:{remote_path}"],
                check=False,
            ).returncode
            if rc != 0:
                return rc
            rc = _ssh_run(ssh_target, f"chmod 600 {remote_path}")
            if rc != 0:
                return rc

    rc = _ssh_run(ssh_target, "systemctl daemon-reload")
    if rc != 0:
        return rc

    if not enable:
        print(
            "Install complete. Timers are not enabled. "
            f"Run with --enable (and review timers) or: ssh {ssh_target} 'systemctl enable --now <timer>'",
            file=sys.stderr,
        )
        return 0

    timers: list[str] = []
    if do_pgbr:
        timers.extend(timers_pgbr)
    if do_restic:
        timers.extend(timers_restic)
    for t in timers:
        rc = _ssh_run(ssh_target, f"systemctl enable --now {t}")
        if rc != 0:
            return rc
    print(f"Enabled timers on {ssh_target}: {' '.join(timers)}", file=sys.stderr)
    return 0
