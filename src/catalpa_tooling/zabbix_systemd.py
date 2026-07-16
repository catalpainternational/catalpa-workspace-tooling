"""Install and manage Zabbix Agent 2 via Docker under systemd."""

from __future__ import annotations

import argparse
import base64
import grp
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from catalpa_tooling.restic_files import RESTIC_IMAGE
from catalpa_tooling.run_cmd import run as run_cmd

UNIT_NAME = "zabbix-agent2.service"
UNIT_DESCRIPTION_LABEL = "project"
CONTAINER_NAME = "zabbix-agent2"
DEFAULT_IMAGE = "zabbix/zabbix-agent2:alpine-latest"
SYSTEMD_UNIT_PATH = Path("/etc/systemd/system") / UNIT_NAME
ENV_DIR = Path("/etc/zabbix")
ENV_FILE = ENV_DIR / "zabbix-agent2.docker.env"
USERPARAMS_FILE = ENV_DIR / "zabbix-agent2-userparams.conf"
CHROOT_BIN = "/usr/sbin/chroot"
CHROOT_HOST_ROOT = "/host/root"
DEFAULT_CHROOT_DOCKER_CLI = "/usr/bin/docker"
DEFAULT_ZABBIX_COMPOSE_PROJECT = ""
DEFAULT_ZABBIX_COMPOSE_DB_SERVICE = "db"
DEFAULT_ZABBIX_COMPOSE_REPLICA = 1
DEFAULT_ZABBIX_RESTIC_DOCKER_ENV_FILE = str(ENV_DIR / "restic-files-backup.env")
ZABBIX_ITEM_KEY_PGBACKREST_INFO = "pgbackrest.info"
ZABBIX_ITEM_KEY_RESTIC_SNAPSHOTS = "restic.snapshots"
DOCKER_GROUP_FALLBACK_GID = 988
DOCKER_SOCK_PATH = Path("/var/run/docker.sock")
DEFAULT_ZBX_SERVER_HOST = "zabbix.catalpa.build"

# ``info.yaml`` ``env:`` keys mapped with ``_yaml_mapping_to_env`` use these for server/hostname/active;
# any other ``ZBX_*`` keys from that block are merged into the agent env file as-is (e.g. ``ZBX_METADATA``).
ZBX_ENV_RESERVED_FOR_MERGE = frozenset(
    {"ZBX_SERVER_HOST", "ZBX_HOSTNAME", "ZBX_ACTIVE_ALLOW"}
)

ENV_TEMPLATE = (
    "# Used by {unit} (docker run --env-file).\n"
    "# https://hub.docker.com/r/zabbix/zabbix-agent2\n"
    f"ZBX_SERVER_HOST={DEFAULT_ZBX_SERVER_HOST}\n"
    "# ZBX_HOSTNAME defaults from info.yaml site_origin (host) unless overridden.\n"
    "ZBX_HOSTNAME=your-docker-host-name\n"
    "ZBX_ACTIVE_ALLOW=true\n"
)


def hostname_from_site_origin(site_origin: str | None) -> str | None:
    """Return hostname for ZBX_HOSTNAME from ``site_origin`` (scheme + host [+ port])."""
    raw = (site_origin or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = parsed.hostname
    return host.strip() if host else None


def _sudo(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    """Run a command as root, prefixing sudo when uid is not 0."""
    if os.geteuid() == 0:
        return run_cmd(argv, **kwargs)
    sud = shutil.which("sudo")
    if not sud:
        print("zabbix: need root or sudo on PATH to manage systemd files.", file=sys.stderr)
        raise SystemExit(1)
    kw = {"print_cmd": True, **kwargs}
    return run_cmd([sud, *argv], **kw)


def _systemctl_present() -> bool:
    return shutil.which("systemctl") is not None


def _require_systemd_host() -> None:
    if not _systemctl_present():
        print(
            "zabbix: `systemctl` not found; these commands target a Linux host with systemd.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def resolve_docker_group_gid_local(*, explicit: int | None) -> int:
    """Pick GID for docker --group-add on this host."""
    if explicit is not None:
        return int(explicit)
    try:
        return int(grp.getgrnam("docker").gr_gid)
    except KeyError:
        pass
    try:
        return int(DOCKER_SOCK_PATH.stat().st_gid)
    except OSError:
        pass
    print(
        f"zabbix: no `docker` group and no {DOCKER_SOCK_PATH}; using fallback GID {DOCKER_GROUP_FALLBACK_GID}.",
        file=sys.stderr,
    )
    return DOCKER_GROUP_FALLBACK_GID


def _unit_file_content(*, image: str, docker_group_gid: int) -> str:
    # UserParameter commands use `chroot /host/root docker …` so the host Docker CLI runs with host libc
    # (the agent image is Alpine/musl and cannot execute the host `docker` binary directly).
    # chroot(2) needs CAP_SYS_CHROOT; run the container as root (--user 0:0).
    userparams_mount = USERPARAMS_FILE.name
    return f"""[Unit]
Description={UNIT_DESCRIPTION_LABEL}: Zabbix Agent 2 (Docker)
Documentation=https://hub.docker.com/r/zabbix/zabbix-agent2
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=simple
Restart=always
RestartSec=10s
TimeoutStartSec=0
ExecStartPre=-/usr/bin/docker pull {image}
ExecStart=/usr/bin/docker run --rm \\
    --name {CONTAINER_NAME} \\
    --privileged \\
    --user 0:0 \\
    --network=host \\
    --group-add {int(docker_group_gid)} \\
    --env-file {ENV_FILE} \\
    -v /var/run/docker.sock:/var/run/docker.sock:ro \\
    -v /proc:/host/proc:ro \\
    -v /sys:/host/sys:ro \\
    -v /:/host/root:ro \\
    -v {USERPARAMS_FILE}:/etc/zabbix/zabbix_agent2.d/{userparams_mount}:ro \\
    {image}
ExecStop=/usr/bin/docker stop -t 30 {CONTAINER_NAME}
ExecStopPost=-/usr/bin/docker rm -f {CONTAINER_NAME}

[Install]
WantedBy=multi-user.target
"""


def _write_text_root(path: Path, content: str, *, mode: int = 0o644) -> None:
    _sudo(["/bin/mkdir", "-p", str(path.parent)], check=True)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
        tmp.write(content)
        tmp.flush()
        tmp_path = Path(tmp.name)
    try:
        _sudo(["/bin/cp", str(tmp_path), str(path)], check=True)
        _sudo(["/bin/chmod", oct(mode)[2:], str(path)], check=True)
    finally:
        tmp_path.unlink(missing_ok=True)


def _read_env_file() -> str | None:
    r = _sudo(
        ["/bin/cat", str(ENV_FILE)],
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode != 0:
        return None
    return r.stdout


def _parse_env_lines(text: str) -> dict[str, str]:
    keys: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        keys[k.strip()] = v.strip()
    return keys


def _format_env_body(keys: dict[str, str]) -> str:
    order = ["ZBX_SERVER_HOST", "ZBX_HOSTNAME", "ZBX_ACTIVE_ALLOW", "ZBX_SERVER_PORT"]
    extras = sorted(k for k in keys if k not in order)
    out: list[str] = ["# Managed by `uv run dk <env> zabbix install` - edit freely.", ""]
    for k in order:
        if k in keys:
            out.append(f"{k}={keys[k]}")
    for k in extras:
        out.append(f"{k}={keys[k]}")
    out.append("")
    return "\n".join(out)


def _extra_zbx_from_info_yaml(env_defaults: dict[str, str] | None) -> dict[str, str]:
    """Return ``ZBX_*`` keys from ``info.yaml`` ``env:`` except server/hostname/active (handled elsewhere)."""
    out: dict[str, str] = {}
    for k, v in (env_defaults or {}).items():
        if k.startswith("ZBX_") and k not in ZBX_ENV_RESERVED_FOR_MERGE:
            out[k] = v
    return out


def _apply_extra_zbx_keys(keys: dict[str, str], env_defaults: dict[str, str] | None) -> None:
    keys.update(_extra_zbx_from_info_yaml(env_defaults))


def _apply_zbx_tls_psk_defaults(keys: dict[str, str]) -> None:
    """If PSK key+identity are set, Agent 2 must use PSK for connections; else it errors at startup.

    See: ``TLSPSKIdentity configuration parameter set without PSK being used`` when
    ``ZBX_TLSCONNECT`` is missing.
    """
    psk = (keys.get("ZBX_TLSPSK") or "").strip()
    ident = (keys.get("ZBX_TLSPSKIDENTITY") or "").strip()
    if psk and ident and not (keys.get("ZBX_TLSCONNECT") or "").strip():
        keys["ZBX_TLSCONNECT"] = "psk"


def _merge_env_keys(
    existing: str | None,
    *,
    server: str | None,
    hostname: str | None,
    active_allow: bool | None,
    env_defaults: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    """Merge CLI/env overrides into env content."""
    extras = _extra_zbx_from_info_yaml(env_defaults)
    overrides = (
        server is not None
        or hostname is not None
        or active_allow is not None
        or bool(extras)
    )
    if existing is None:
        body = ENV_TEMPLATE.format(unit=UNIT_NAME)
        keys = _parse_env_lines(body)
        if server is not None:
            keys["ZBX_SERVER_HOST"] = server
        if hostname is not None:
            keys["ZBX_HOSTNAME"] = hostname
        if active_allow is not None:
            keys["ZBX_ACTIVE_ALLOW"] = "true" if active_allow else "false"
        _apply_extra_zbx_keys(keys, env_defaults)
        _apply_zbx_tls_psk_defaults(keys)
        return _format_env_body(keys), "created"
    if overrides:
        keys = _parse_env_lines(existing)
        if server is not None:
            keys["ZBX_SERVER_HOST"] = server
        if hostname is not None:
            keys["ZBX_HOSTNAME"] = hostname
        if active_allow is not None:
            keys["ZBX_ACTIVE_ALLOW"] = "true" if active_allow else "false"
        _apply_extra_zbx_keys(keys, env_defaults)
        _apply_zbx_tls_psk_defaults(keys)
        return _format_env_body(keys), "updated"
    return None, "unchanged"


def _print_install_dry_run_env_preview(
    *,
    existing: str | None,
    new_body: str | None,
    reason: str,
    remote: bool,
) -> None:
    """Print merged env file content for ``install --dry-run`` (matches non-dry-run merge)."""
    scope = "remote " if remote else ""
    labels = {
        "created": "would create",
        "updated": "would update",
        "unchanged": "unchanged (no write)",
    }
    print(f"[dry-run] {scope}{ENV_FILE} — {labels[reason]}", flush=True)
    content = new_body if new_body is not None else existing
    if content:
        print("[dry-run] env file contents:\n" + content, flush=True)
    else:
        print("[dry-run] env file contents: (empty)", flush=True)


def _ensure_env_file(
    *,
    server: str | None,
    hostname: str | None,
    active_allow: bool | None,
    dry_run: bool,
    env_defaults: dict[str, str] | None = None,
) -> None:
    if dry_run:
        print(f"[dry-run] would mkdir -p {ENV_DIR} and ensure {ENV_FILE}", flush=True)
        existing = _read_env_file()
        new_body, reason = _merge_env_keys(
            existing,
            server=server,
            hostname=hostname,
            active_allow=active_allow,
            env_defaults=env_defaults,
        )
        _print_install_dry_run_env_preview(
            existing=existing, new_body=new_body, reason=reason, remote=False
        )
        return
    _sudo(["/bin/mkdir", "-p", str(ENV_DIR)], check=True)
    existing = _read_env_file()
    new_body, reason = _merge_env_keys(
        existing,
        server=server,
        hostname=hostname,
        active_allow=active_allow,
        env_defaults=env_defaults,
    )
    if new_body is not None:
        _write_text_root(ENV_FILE, new_body, mode=0o600)
        if reason == "created":
            print(f"Created {ENV_FILE}.", flush=True)
        else:
            print(f"Updated {ENV_FILE}.", flush=True)


def cmd_install(
    *,
    image: str,
    server: str | None,
    hostname: str | None,
    active_allow: bool | None,
    dry_run: bool,
    docker_group_gid: int | None = None,
    env_defaults: dict[str, str] | None = None,
) -> int:
    _require_systemd_host()
    dgid = resolve_docker_group_gid_local(explicit=docker_group_gid)
    print(f"zabbix: docker run will use --group-add {dgid}.", flush=True)
    _ensure_env_file(
        server=server,
        hostname=hostname,
        active_allow=active_allow,
        dry_run=dry_run,
        env_defaults=env_defaults,
    )
    unit_body = _unit_file_content(image=image, docker_group_gid=dgid)
    if dry_run:
        print(f"[dry-run] unit -> {SYSTEMD_UNIT_PATH}", flush=True)
        print("[dry-run] unit file contents:\n" + unit_body, flush=True)
        return 0
    _write_text_root(SYSTEMD_UNIT_PATH, unit_body, mode=0o644)
    _sudo(["/bin/systemctl", "daemon-reload"], check=True)
    print(
        f"Installed {SYSTEMD_UNIT_PATH}. Run `uv run dk <env> zabbix enable` to start.",
        flush=True,
    )
    return 0


def cmd_enable(*, dry_run: bool) -> int:
    _require_systemd_host()
    if _read_env_file() is None:
        print(f"zabbix: missing {ENV_FILE}; run `uv run dk <env> zabbix install` first.", file=sys.stderr)
        return 1
    if dry_run:
        print(f"[dry-run] systemctl enable --now {UNIT_NAME}", flush=True)
        return 0
    r = _sudo(["/bin/systemctl", "enable", "--now", UNIT_NAME], check=False)
    return int(r.returncode or 0)


def cmd_disable(*, dry_run: bool) -> int:
    _require_systemd_host()
    if dry_run:
        print(f"[dry-run] systemctl disable --now {UNIT_NAME}", flush=True)
        return 0
    r = _sudo(["/bin/systemctl", "disable", "--now", UNIT_NAME], check=False)
    return int(r.returncode or 0)


def cmd_restart(*, dry_run: bool) -> int:
    """Restart the unit so ``docker run`` reloads ``--env-file`` after ``install`` updates."""
    _require_systemd_host()
    if _read_env_file() is None:
        print(f"zabbix: missing {ENV_FILE}; run `uv run dk <env> zabbix install` first.", file=sys.stderr)
        return 1
    if dry_run:
        print(f"[dry-run] systemctl restart {UNIT_NAME}", flush=True)
        return 0
    r = _sudo(["/bin/systemctl", "restart", UNIT_NAME], check=False)
    return int(r.returncode or 0)


def cmd_logs(*, lines: int, follow: bool) -> int:
    _require_systemd_host()
    j_base = ["/bin/journalctl", "-u", UNIT_NAME, "-n", str(lines), "--no-pager"]
    if follow:
        j_base.append("-f")
    print("--- journal (systemd / docker) ---", flush=True)
    r = _sudo(j_base, check=False)
    return int(r.returncode or 0)


def _ssh_cmd(ssh_target: str, remote_argv: list[str], *, tty: bool = False) -> list[str]:
    cmd = ["ssh", "-o", "BatchMode=yes"]
    if tty:
        cmd.append("-t")
    cmd.append(ssh_target)
    cmd.extend(remote_argv)
    return cmd


def resolve_docker_group_gid_remote(ssh_target: str, *, explicit: int | None) -> int:
    """Pick GID for docker --group-add on remote host."""
    if explicit is not None:
        return int(explicit)
    r = run_cmd(
        _ssh_cmd(ssh_target, ["getent", "group", "docker"]),
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode == 0 and (r.stdout or "").strip():
        line = (r.stdout or "").strip().splitlines()[0]
        parts = line.split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            return int(parts[2])
    r2 = run_cmd(
        _ssh_cmd(ssh_target, ["stat", "-c", "%g", str(DOCKER_SOCK_PATH)]),
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r2.returncode == 0 and (r2.stdout or "").strip().isdigit():
        return int((r2.stdout or "").strip())
    print(
        f"zabbix: remote host has no docker group / readable socket; using fallback GID {DOCKER_GROUP_FALLBACK_GID}.",
        file=sys.stderr,
    )
    return DOCKER_GROUP_FALLBACK_GID


def _read_env_file_remote(ssh_target: str) -> str | None:
    r = run_cmd(
        _ssh_cmd(ssh_target, [f"sudo test -f {ENV_FILE} && sudo cat {ENV_FILE} || true"]),
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return out if out else None


def _remote_install_script(
    *,
    env_body: str | None,
    unit_body: str,
    userparams_body: str,
) -> str:
    unit_b64 = base64.b64encode(unit_body.encode("utf-8")).decode("ascii")
    up_b64 = base64.b64encode(userparams_body.encode("utf-8")).decode("ascii")
    lines = ["set -euo pipefail", f"sudo mkdir -p {ENV_DIR}"]
    if env_body is not None:
        env_b64 = base64.b64encode(env_body.encode("utf-8")).decode("ascii")
        lines.append(f"echo '{env_b64}' | base64 -d | sudo tee {ENV_FILE} > /dev/null")
        lines.append(f"sudo chmod 600 {ENV_FILE}")
    lines.append(f"echo '{up_b64}' | base64 -d | sudo tee {USERPARAMS_FILE} > /dev/null")
    lines.append(f"sudo chmod 644 {USERPARAMS_FILE}")
    lines.append(f"echo '{unit_b64}' | base64 -d | sudo tee {SYSTEMD_UNIT_PATH} > /dev/null")
    lines.append(f"sudo chmod 644 {SYSTEMD_UNIT_PATH}")
    lines.append("sudo systemctl daemon-reload")
    return "\n".join(lines) + "\n"


def cmd_install_remote(
    ssh_target: str,
    *,
    image: str,
    server: str | None,
    hostname: str | None,
    active_allow: bool | None,
    dry_run: bool,
    docker_group_gid: int | None = None,
    env_defaults: dict[str, str] | None = None,
) -> int:
    dgid = resolve_docker_group_gid_remote(ssh_target, explicit=docker_group_gid)
    print(
        f"zabbix: remote install via {ssh_target!r} (--group-add {dgid} on deploy host).",
        flush=True,
    )
    if dry_run:
        existing = _read_env_file_remote(ssh_target)
        new_env, env_reason = _merge_env_keys(
            existing,
            server=server,
            hostname=hostname,
            active_allow=active_allow,
            env_defaults=env_defaults,
        )
        userparams_body = render_userparams_conf(env_defaults or {})
        unit_body = _unit_file_content(image=image, docker_group_gid=dgid)
        print("[dry-run] would write env + unit on remote and run daemon-reload", flush=True)
        _print_install_dry_run_env_preview(
            existing=existing, new_body=new_env, reason=env_reason, remote=True
        )
        print(f"[dry-run] {USERPARAMS_FILE} — contents:\n{userparams_body}", flush=True)
        print(f"[dry-run] unit -> {SYSTEMD_UNIT_PATH}", flush=True)
        print("[dry-run] unit file contents:\n" + unit_body, flush=True)
        return 0

    existing = _read_env_file_remote(ssh_target)
    new_env, env_reason = _merge_env_keys(
        existing,
        server=server,
        hostname=hostname,
        active_allow=active_allow,
        env_defaults=env_defaults,
    )
    userparams_body = render_userparams_conf(env_defaults or {})
    unit_body = _unit_file_content(image=image, docker_group_gid=dgid)
    script = _remote_install_script(
        env_body=new_env,
        unit_body=unit_body,
        userparams_body=userparams_body,
    )
    r = run_cmd(
        _ssh_cmd(ssh_target, ["bash", "-s"]),
        input=script,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return int(r.returncode or 1)
    if new_env is not None:
        print(f"Remote {ENV_FILE} ({env_reason}).", flush=True)
    else:
        print(f"Remote {ENV_FILE} unchanged.", flush=True)
    print(f"Remote {USERPARAMS_FILE} (UserParameter fragment from info.yaml).", flush=True)
    print(f"Remote {SYSTEMD_UNIT_PATH} installed.", flush=True)
    return 0


def _env_bool(env: dict[str, str], key: str, *, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    return default


def _compose_project_for_zabbix(env: dict[str, str]) -> str:
    return _nonempty_env_str(env, "COMPOSE_PROJECT_NAME") or DEFAULT_ZABBIX_COMPOSE_PROJECT


def _compose_replica(env: dict[str, str]) -> int:
    raw = (env.get("ZABBIX_COMPOSE_CONTAINER_REPLICA") or "").strip()
    if not raw:
        return DEFAULT_ZABBIX_COMPOSE_REPLICA
    try:
        n = int(raw, 10)
    except ValueError:
        print(
            f"zabbix: ZABBIX_COMPOSE_CONTAINER_REPLICA must be an integer; ignoring {raw!r}.",
            file=sys.stderr,
        )
        return DEFAULT_ZABBIX_COMPOSE_REPLICA
    return n if n > 0 else DEFAULT_ZABBIX_COMPOSE_REPLICA


def _compose_naming_legacy(env: dict[str, str]) -> bool:
    mode = (env.get("ZABBIX_COMPOSE_CONTAINER_NAMING") or "").strip().lower()
    if mode in ("legacy", "v1", "compat", "underscore", "underscores"):
        return True
    if mode in ("v2", "hyphen", "modern", ""):
        return False
    print(
        f"zabbix: ZABBIX_COMPOSE_CONTAINER_NAMING={mode!r} unknown; "
        "use legacy|v2 (default v2). Assuming v2.",
        file=sys.stderr,
    )
    return False


def compose_default_container_name(
    project: str,
    service: str,
    *,
    replica: int = 1,
    legacy_underscore: bool = False,
) -> str:
    p = (project or "").strip() or DEFAULT_ZABBIX_COMPOSE_PROJECT
    s = (service or "").strip()
    r = int(replica) if replica > 0 else DEFAULT_ZABBIX_COMPOSE_REPLICA
    if legacy_underscore:
        return f"{p}_{s}_{r}"
    return f"{p}-{s}-{r}"


def _resolved_monitor_container(
    env: dict[str, str],
    *,
    explicit_key: str,
    service_env_key: str,
    default_service: str,
) -> str | None:
    explicit = _nonempty_env_str(env, explicit_key)
    if explicit:
        return explicit
    project = _compose_project_for_zabbix(env)
    legacy = _compose_naming_legacy(env)
    replica = _compose_replica(env)
    service = _nonempty_env_str(env, service_env_key) or default_service
    return compose_default_container_name(
        project, service, replica=replica, legacy_underscore=legacy
    )


def _chroot_host_docker_bin(env: dict[str, str]) -> str:
    raw = (env.get("ZABBIX_CHROOT_DOCKER_CLI") or "").strip()
    if raw.startswith("/"):
        return raw
    if raw:
        print(
            "zabbix: ZABBIX_CHROOT_DOCKER_CLI must be an absolute path; "
            f"ignoring {raw!r}, using {DEFAULT_CHROOT_DOCKER_CLI!r}.",
            file=sys.stderr,
        )
    return DEFAULT_CHROOT_DOCKER_CLI


def _restic_snapshots_userparameter_command(env: dict[str, str]) -> str:
    env_file = _nonempty_env_str(env, "ZABBIX_RESTIC_DOCKER_ENV_FILE") or DEFAULT_ZABBIX_RESTIC_DOCKER_ENV_FILE
    image = _nonempty_env_str(env, "ZABBIX_RESTIC_DOCKER_IMAGE") or RESTIC_IMAGE
    docker_bin = shlex.quote(_chroot_host_docker_bin(env))
    chroot_prefix = f"{CHROOT_BIN} {CHROOT_HOST_ROOT} {docker_bin}"
    parts: list[str] = [chroot_prefix, "run", "--rm"]
    if _env_bool(env, "ZABBIX_RESTIC_DOCKER_PLATFORM_AMD64", default=True):
        parts.extend(["--platform", "linux/amd64"])
    from catalpa_tooling.dc_backup.hosts import (
        DC_BACKUP_CA_CONTAINER_PATH,
        dc_backup_ca_host_path,
        parse_docker_add_hosts,
    )

    try:
        for name, ip in parse_docker_add_hosts(env):
            parts.extend(["--add-host", shlex.quote(f"{name}:{ip}")])
        ca = dc_backup_ca_host_path(env)
    except ValueError:
        ca = None
    if ca:
        parts.extend(["-v", shlex.quote(f"{ca}:{DC_BACKUP_CA_CONTAINER_PATH}:ro")])
        parts.extend(["-e", shlex.quote(f"AWS_CA_BUNDLE={DC_BACKUP_CA_CONTAINER_PATH}")])
    parts.extend(["--env-file", shlex.quote(env_file), shlex.quote(image)])
    if ca:
        parts.extend(["--cacert", shlex.quote(DC_BACKUP_CA_CONTAINER_PATH)])
    parts.extend(["--json", "snapshots"])
    return " ".join(parts)


def render_userparams_conf(env: dict[str, str]) -> str:
    """Fragment for ``zabbix_agent2.d`` with ``UserParameter`` lines."""
    lines: list[str] = [
        "# Managed by `uv run dk <env> zabbix install`.",
        "# Regenerate after changing docker/envs/<env>/info.yaml (compose_project_name, zabbix_*).",
        "#",
        "# Test on host (no chroot):",
        "#   docker exec -i -u postgres <db-container> pgbackrest info --output=json",
        f"#   docker run --rm --platform linux/amd64 --env-file {DEFAULT_ZABBIX_RESTIC_DOCKER_ENV_FILE} \\",
        f"#     {RESTIC_IMAGE} --json snapshots",
        "",
    ]
    docker_bin = shlex.quote(_chroot_host_docker_bin(env))
    chroot_prefix = f"{CHROOT_BIN} {CHROOT_HOST_ROOT} {docker_bin}"

    show_pg = _env_bool(env, "ZABBIX_USERPARAMETER_PGBACKREST", default=True)
    show_restic = _env_bool(env, "ZABBIX_USERPARAMETER_RESTIC", default=True)

    db_c = (
        _resolved_monitor_container(
            env,
            explicit_key="ZABBIX_DOCKER_DB_CONTAINER",
            service_env_key="ZABBIX_COMPOSE_DB_SERVICE",
            default_service=DEFAULT_ZABBIX_COMPOSE_DB_SERVICE,
        )
        if show_pg
        else None
    )
    if db_c:
        lines.append(
            f"UserParameter={ZABBIX_ITEM_KEY_PGBACKREST_INFO},"
            f"{chroot_prefix} exec -i -u postgres {shlex.quote(db_c)} pgbackrest info --output=json"
        )

    if show_restic:
        lines.append(
            f"UserParameter={ZABBIX_ITEM_KEY_RESTIC_SNAPSHOTS},"
            f"{_restic_snapshots_userparameter_command(env)}"
        )

    if not db_c and not show_restic:
        lines.append(
            "# No UserParameter lines: set zabbix_userparameter_pgbackrest / "
            "zabbix_userparameter_restic (defaults true) and compose_project_name, "
            "or set zabbix_docker_db_container."
        )
    lines.append("")
    return "\n".join(lines)


def cmd_enable_remote(ssh_target: str, *, dry_run: bool) -> int:
    print(f"zabbix: remote enable via {ssh_target!r}", flush=True)
    if _read_env_file_remote(ssh_target) is None:
        print(f"zabbix: missing {ENV_FILE} on remote; run `... zabbix install` first.", file=sys.stderr)
        return 1
    if dry_run:
        print(f"[dry-run] sudo systemctl enable --now {UNIT_NAME}", flush=True)
        return 0
    r = run_cmd(
        _ssh_cmd(ssh_target, ["sudo", "/bin/systemctl", "enable", "--now", UNIT_NAME]),
        check=False,
    )
    return int(r.returncode or 0)


def cmd_disable_remote(ssh_target: str, *, dry_run: bool) -> int:
    print(f"zabbix: remote disable via {ssh_target!r}", flush=True)
    if dry_run:
        print(f"[dry-run] sudo systemctl disable --now {UNIT_NAME}", flush=True)
        return 0
    r = run_cmd(
        _ssh_cmd(ssh_target, ["sudo", "/bin/systemctl", "disable", "--now", UNIT_NAME]),
        check=False,
    )
    return int(r.returncode or 0)


def cmd_restart_remote(ssh_target: str, *, dry_run: bool) -> int:
    print(f"zabbix: remote restart via {ssh_target!r}", flush=True)
    if _read_env_file_remote(ssh_target) is None:
        print(f"zabbix: missing {ENV_FILE} on remote; run `... zabbix install` first.", file=sys.stderr)
        return 1
    if dry_run:
        print(f"[dry-run] sudo systemctl restart {UNIT_NAME}", flush=True)
        return 0
    r = run_cmd(
        _ssh_cmd(ssh_target, ["sudo", "/bin/systemctl", "restart", UNIT_NAME]),
        check=False,
    )
    return int(r.returncode or 0)


def cmd_logs_remote(ssh_target: str, *, lines: int, follow: bool) -> int:
    print(f"zabbix: remote logs via {ssh_target!r}", flush=True)
    j = [
        "sudo",
        "/bin/journalctl",
        "-u",
        UNIT_NAME,
        "-n",
        str(lines),
        "--no-pager",
    ]
    if follow:
        j.append("-f")
    ssh_cmd = _ssh_cmd(ssh_target, j, tty=follow)
    return int(subprocess.call(ssh_cmd))


def _nonempty_env_str(env: dict[str, str], key: str) -> str | None:
    v = (env.get(key) or "").strip()
    return v if v else None


def _active_allow_from_env(env: dict[str, str]) -> bool:
    """YAML/env ``ZBX_ACTIVE_ALLOW`` or default **true** when unset."""
    raw = (env.get("ZBX_ACTIVE_ALLOW") or "").strip()
    if not raw:
        return True
    low = raw.lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    return True


def _install_configuration_guard_passed(
    env_defaults: dict[str, str] | None,
    *,
    args_server: str | None,
    args_hostname: str | None,
    args_active_allow: bool | None,
    force: bool,
) -> bool:
    """Require at least one ``ZBX_*`` in merged deploy/env defaults or explicit install CLI flags."""
    if force:
        return True
    ed = env_defaults or {}
    if any(k.startswith("ZBX_") for k in ed):
        return True
    if (
        args_server is not None
        or args_hostname is not None
        or args_active_allow is not None
    ):
        return True
    return False


def _install_options_from_env_and_cli(
    *,
    args_server: str | None,
    args_hostname: str | None,
    args_active_allow: bool | None,
    env_defaults: dict[str, str] | None,
    site_origin: str | None,
) -> tuple[str | None, str | None, bool]:
    """CLI wins; then info.yaml ``env:``; hostname falls back to ``site_origin`` host."""
    env = env_defaults or {}
    server = (
        args_server if args_server is not None else _nonempty_env_str(env, "ZBX_SERVER_HOST")
    )
    hostname = args_hostname if args_hostname is not None else _nonempty_env_str(env, "ZBX_HOSTNAME")
    if hostname is None:
        hostname = hostname_from_site_origin(site_origin)
    active_allow = (
        args_active_allow if args_active_allow is not None else _active_allow_from_env(env)
    )
    return server, hostname, active_allow


def build_zabbix_argparser(*, prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "Install or control Zabbix Agent 2 (official Docker image) via a systemd unit on "
            "the env target host. UserParameter keys: pgbackrest.info (docker exec into db), "
            "restic.snapshots (docker run --rm + restic-files-backup.env). "
            "Toggle with zabbix_userparameter_pgbackrest / zabbix_userparameter_restic in info.yaml env."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser(
        "install",
        help=f"Write {UNIT_NAME}, {ENV_FILE.name}, {USERPARAMS_FILE.name} under {ENV_DIR}; systemctl daemon-reload.",
    )
    p_install.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="Agent image for pull/run (re-run install to change).",
    )
    p_install.add_argument(
        "--server",
        metavar="HOST",
        default=None,
        help=f"Set ZBX_SERVER_HOST (default on fresh install: {DEFAULT_ZBX_SERVER_HOST}).",
    )
    p_install.add_argument(
        "--hostname",
        metavar="NAME",
        default=None,
        help=(
            "Set ZBX_HOSTNAME (must match the host in Zabbix). "
            "If omitted, defaults to the host part of site_origin in info.yaml."
        ),
    )
    p_install.add_argument(
        "--active-allow",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="Set or clear ZBX_ACTIVE_ALLOW (omit to leave unchanged for existing env file).",
    )
    p_install.add_argument(
        "--docker-group-gid",
        type=int,
        default=None,
        metavar="GID",
        help=(
            "Supplementary GID for docker --group-add. Default: docker group, then socket gid, then 988."
        ),
    )
    p_install.add_argument("--dry-run", action="store_true")
    p_install.add_argument(
        "--force",
        action="store_true",
        help=(
            "Allow install when no zbx_* keys are set in info.yaml env (and no ZBX_* from "
            "credentials), and no --server / --hostname / --active-allow were passed."
        ),
    )

    p_en = sub.add_parser("enable", help=f"systemctl enable --now {UNIT_NAME}")
    p_en.add_argument("--dry-run", action="store_true")

    p_dis = sub.add_parser("disable", help=f"systemctl disable --now {UNIT_NAME}")
    p_dis.add_argument("--dry-run", action="store_true")

    p_rst = sub.add_parser(
        "restart",
        help=f"systemctl restart {UNIT_NAME} (reload env after `install` updates).",
    )
    p_rst.add_argument("--dry-run", action="store_true")

    p_logs = sub.add_parser("logs", help=f"journalctl for {UNIT_NAME}")
    p_logs.add_argument("-n", "--lines", type=int, default=100, metavar="N")
    p_logs.add_argument("-f", "--follow", action="store_true")
    return parser


def _apply_config_globals(config: ProjectConfig) -> None:
    """Bind module paths and unit names from ``tooling.yaml`` (single-threaded CLI)."""
    global UNIT_NAME, UNIT_DESCRIPTION_LABEL, SYSTEMD_UNIT_PATH, ENV_DIR, ENV_FILE, USERPARAMS_FILE
    global DEFAULT_ZABBIX_COMPOSE_PROJECT, DEFAULT_ZABBIX_RESTIC_DOCKER_ENV_FILE
    UNIT_NAME = config.ops.zabbix.unit_name
    UNIT_DESCRIPTION_LABEL = config.meta.name
    SYSTEMD_UNIT_PATH = Path("/etc/systemd/system") / UNIT_NAME
    ENV_DIR = Path(config.ops.config_dir)
    ENV_FILE = ENV_DIR / "zabbix-agent2.docker.env"
    USERPARAMS_FILE = ENV_DIR / config.ops.zabbix.userparams_file
    DEFAULT_ZABBIX_COMPOSE_PROJECT = config.stack.compose_project_default
    DEFAULT_ZABBIX_RESTIC_DOCKER_ENV_FILE = str(ENV_DIR / "restic-files-backup.env")


def run_zabbix_deploy(
    argv: list[str],
    *,
    config: "ProjectConfig | None" = None,
    prog: str,
    ssh_target: str | None,
    dry_run: bool,
    env_defaults: dict[str, str] | None = None,
    site_origin: str | None = None,
) -> int:
    """Parse argv and dispatch zabbix subcommands locally or via ssh target."""
    from catalpa_tooling.config import ProjectConfig

    if config is None:
        config = ProjectConfig.from_cwd()
    _apply_config_globals(config)

    args = build_zabbix_argparser(prog=prog).parse_args(argv)
    eff_dry = dry_run or bool(getattr(args, "dry_run", False))

    if args.command == "install":
        if not _install_configuration_guard_passed(
            env_defaults,
            args_server=args.server,
            args_hostname=args.hostname,
            args_active_allow=args.active_allow,
            force=bool(getattr(args, "force", False)),
        ):
            print(
                "zabbix install: no Zabbix configuration — refusing to run.\n"
                "  Add at least one `zbx_*` key under `env:` in docker/envs/<env>/info.yaml "
                "(maps to ZBX_*), and/or `ZBX_*` secrets from decrypted credentials.yaml "
                "(merged into this command).\n"
                "  Or pass --server, --hostname, or --active-allow / --no-active-allow.\n"
                "  TLS PSK: use zbx_tlspsk / zbx_tlspskidentity in credentials.yaml only.\n"
                "  Use --force only if you intentionally install with defaults only.",
                file=sys.stderr,
            )
            return 1

    install_server, install_hostname, install_active = _install_options_from_env_and_cli(
        args_server=args.server if args.command == "install" else None,
        args_hostname=args.hostname if args.command == "install" else None,
        args_active_allow=args.active_allow if args.command == "install" else None,
        env_defaults=env_defaults,
        site_origin=site_origin,
    )

    if ssh_target:
        if args.command == "install":
            return cmd_install_remote(
                ssh_target,
                image=args.image,
                server=install_server,
                hostname=install_hostname,
                active_allow=install_active,
                dry_run=eff_dry,
                docker_group_gid=args.docker_group_gid,
                env_defaults=env_defaults,
            )
        if args.command == "enable":
            return cmd_enable_remote(ssh_target, dry_run=eff_dry)
        if args.command == "disable":
            return cmd_disable_remote(ssh_target, dry_run=eff_dry)
        if args.command == "restart":
            return cmd_restart_remote(ssh_target, dry_run=eff_dry)
        if args.command == "logs":
            return cmd_logs_remote(ssh_target, lines=args.lines, follow=bool(args.follow))
        return 2

    if args.command == "install":
        return cmd_install(
            image=args.image,
            server=install_server,
            hostname=install_hostname,
            active_allow=install_active,
            dry_run=eff_dry,
            docker_group_gid=args.docker_group_gid,
            env_defaults=env_defaults,
        )
    if args.command == "enable":
        return cmd_enable(dry_run=eff_dry)
    if args.command == "disable":
        return cmd_disable(dry_run=eff_dry)
    if args.command == "restart":
        return cmd_restart(dry_run=eff_dry)
    if args.command == "logs":
        return cmd_logs(lines=args.lines, follow=bool(args.follow))
    return 2
