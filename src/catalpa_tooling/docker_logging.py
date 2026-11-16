"""Host-wide Docker log rotation (``/etc/docker/daemon.json``) for deploy hosts (#109).

Docker's default is the ``json-file`` driver with no ``max-size``: every container's
stdout/stderr grows without limit until the container is recreated. App containers reset on
each deploy, but long-lived ones (db, redis, caddy, backup) are often never recreated, and
their logs fill the disk. The droplet bootstrap now writes :data:`DAEMON_LOG_CONFIG`; this
module checks a host against it and applies it to hosts provisioned before that.

``daemon.json`` only affects containers *created* after dockerd restarts with it, so a host
is only fully covered once every container has been recreated — the check reports the
ones that still carry an uncapped log.
"""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass, field
from typing import Any

DAEMON_JSON_PATH = "/etc/docker/daemon.json"

# ``local`` rotates and compresses by default and is what ``docker logs`` reads, so the
# command works unchanged. The opts are spelled out (they match local's defaults) so the
# retention a project can rely on is stated here rather than inherited from Docker.
# Retention: at most 5 files of 20 MB per container, i.e. ~100 MB of log on disk.
LOG_DRIVER = "local"
LOG_OPTS: dict[str, str] = {"max-size": "20m", "max-file": "5"}
DAEMON_LOG_CONFIG: dict[str, Any] = {"log-driver": LOG_DRIVER, "log-opts": LOG_OPTS}

_INSPECT_FORMAT = "{{.Name}}\t{{.HostConfig.LogConfig.Type}}\t{{json .HostConfig.LogConfig.Config}}"


@dataclass(frozen=True)
class ContainerLogConfig:
    name: str
    driver: str
    opts: dict[str, str]

    @property
    def uncapped(self) -> bool:
        """True for a ``json-file`` log with no ``max-size`` — the one that grows forever.

        ``local`` rotates even without opts; other drivers (journald, syslog, none) do not
        write under ``/var/lib/docker/containers`` at all.
        """
        return self.driver == "json-file" and not self.opts.get("max-size")


@dataclass
class HostLogStatus:
    driver: str
    daemon_json: dict[str, Any] | None
    containers: list[ContainerLogConfig] = field(default_factory=list)

    @property
    def daemon_ok(self) -> bool:
        return self.driver == LOG_DRIVER and daemon_config_matches(self.daemon_json)

    @property
    def uncapped(self) -> list[ContainerLogConfig]:
        return [c for c in self.containers if c.uncapped]


def daemon_config_matches(daemon_json: dict[str, Any] | None) -> bool:
    if not daemon_json:
        return False
    opts = daemon_json.get("log-opts") or {}
    return daemon_json.get("log-driver") == LOG_DRIVER and all(
        str(opts.get(k)) == v for k, v in LOG_OPTS.items()
    )


def merge_daemon_config(existing: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``existing`` with the log driver and opts set; other keys are kept.

    ``log-opts`` is replaced, not merged: opts belong to a driver, and an opt left over from
    a previous driver can stop dockerd from starting.
    """
    merged = dict(existing or {})
    merged["log-driver"] = LOG_DRIVER
    merged["log-opts"] = dict(LOG_OPTS)
    return merged


def render_daemon_json(cfg: dict[str, Any]) -> str:
    return json.dumps(cfg, indent=2) + "\n"


def parse_daemon_json(text: str) -> dict[str, Any] | None:
    """Parse ``daemon.json``; ``None`` for an empty file. Raises ``ValueError`` if invalid."""
    if not text.strip():
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{DAEMON_JSON_PATH} is not a JSON object")
    return data


def parse_container_log_configs(text: str) -> list[ContainerLogConfig]:
    """Parse ``docker inspect --format`` output built from :data:`_INSPECT_FORMAT`."""
    out: list[ContainerLogConfig] = []
    for raw in (text or "").splitlines():
        parts = raw.split("\t")
        if len(parts) != 3:
            continue
        name, driver, opts_json = parts
        try:
            opts = json.loads(opts_json) or {}
        except json.JSONDecodeError:
            opts = {}
        out.append(ContainerLogConfig(name.lstrip("/"), driver.strip(), opts))
    return out


def probe_host_logging(ssh_target: str) -> HostLogStatus | None:
    """Read the daemon's log driver, ``daemon.json`` and each container's log config."""
    from catalpa_tooling.dc_backup.ssh_install import remote_run_capture

    info = remote_run_capture(ssh_target, "docker info --format '{{.LoggingDriver}}'")
    if info.returncode != 0:
        print(
            f"could not read docker logging driver on {ssh_target}: "
            f"{(info.stderr or info.stdout).strip()}",
            file=sys.stderr,
        )
        return None

    daemon = remote_run_capture(
        ssh_target, f"if [ -f {DAEMON_JSON_PATH} ]; then cat {DAEMON_JSON_PATH}; fi"
    )
    try:
        daemon_json = parse_daemon_json(daemon.stdout if daemon.returncode == 0 else "")
    except ValueError as e:
        print(f"{DAEMON_JSON_PATH} on {ssh_target} is not valid JSON: {e}", file=sys.stderr)
        return None

    inspect = remote_run_capture(
        ssh_target,
        f"docker ps -aq | xargs -r docker inspect --format {shlex.quote(_INSPECT_FORMAT)}",
    )
    containers = parse_container_log_configs(inspect.stdout if inspect.returncode == 0 else "")
    return HostLogStatus(info.stdout.strip(), daemon_json, containers)


def report_host_logging(label: str, status: HostLogStatus, *, env_name: str) -> bool:
    """Print a summary; return True when the daemon and every container are capped."""
    opts = (status.daemon_json or {}).get("log-opts") or {}
    opts_s = ", ".join(f"{k}={v}" for k, v in sorted(opts.items())) or "none"
    print(f"remote {label}: docker log driver={status.driver}; log-opts: {opts_s}", flush=True)
    ok = True
    if not status.daemon_ok:
        ok = False
        print(
            f"warning: remote {label} docker daemon does not cap container logs "
            f"(want {LOG_DRIVER} with {LOG_OPTS}). Fix: dk {env_name} host logging --apply"
            + (" --target backup" if label == "dc_backup" else ""),
            file=sys.stderr,
        )
    uncapped = status.uncapped
    if uncapped:
        ok = False
        names = ", ".join(c.name for c in uncapped)
        print(
            f"warning: remote {label}: {len(uncapped)} container(s) still log without a cap "
            f"(created before the daemon default changed): {names}. "
            "Recreate them once, e.g. "
            + (
                f"dk {env_name} up -d --force-recreate"
                if label == "app"
                else "docker compose up -d --force-recreate in each stack on that host"
            ),
            file=sys.stderr,
        )
    return ok


def _ssh_target_for(info: dict[str, Any], env_name: str, target: str) -> str | None:
    from catalpa_tooling.dc_backup.paths import INFO_DC_BACKUP_DOCKER_HOST
    from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

    key = INFO_DC_BACKUP_DOCKER_HOST if target == "backup" else "docker_host"
    raw = str(info.get(key) or "").strip()
    if not raw:
        print(f"{key} is not set in docker/envs/{env_name}/info.yaml.", file=sys.stderr)
        return None
    try:
        return parse_docker_host_to_ssh_target(raw)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return None


def _write_daemon_json(ssh_target: str, body: str, *, had_file: bool) -> int:
    """Stage, validate, back up and install ``daemon.json``; the live file is untouched on error."""
    from catalpa_tooling.run_cmd import run as run_cmd

    staged = f"{DAEMON_JSON_PATH}.catalpa-new"
    backup = f"cp -a {DAEMON_JSON_PATH} {DAEMON_JSON_PATH}.bak-$(date +%Y%m%d%H%M%S) && " if had_file else ""
    script = (
        "set -e; "
        "mkdir -p /etc/docker; "
        f"cat > {staged}; "
        f"chmod 0644 {staged}; "
        f"if ! dockerd --validate --config-file {staged}; then rm -f {staged}; exit 1; fi; "
        f"{backup}mv {staged} {DAEMON_JSON_PATH}"
    )
    r = run_cmd(
        ["ssh", "-o", "BatchMode=yes", ssh_target, f"sudo sh -c {shlex.quote(script)}"],
        check=False,
        print_cmd=True,
        input=body,
        text=True,
    )
    return int(r.returncode or 0)


def cmd_host_logging(
    config: Any,
    env_name: str,
    *,
    target: str = "app",
    apply: bool = False,
    dry_run: bool = False,
    yes: bool = False,
) -> int:
    """``dk <env> host logging [--apply]``: check, or install, the host's log rotation default."""
    from catalpa_tooling.cli_confirm import confirm_by_typing_env_name
    from catalpa_tooling.deploy_do_link import load_env_info
    from catalpa_tooling.dc_backup.ssh_install import remote_run_capture
    from catalpa_tooling.ssh_known_hosts import ensure_ssh_known_host_for_docker_host

    loaded = load_env_info(config, env_name)
    if loaded is None:
        return 1
    _, info = loaded
    from catalpa_tooling.systemd_remote_install import rootless_podman_socket

    if target == "app" and rootless_podman_socket(str(info.get("docker_host") or "")):
        print(
            f"host logging: {env_name} runs rootless Podman, which has no {DAEMON_JSON_PATH} "
            "to set; its log driver comes from containers.conf. Nothing to check or install."
        )
        return 0
    ssh = _ssh_target_for(info, env_name, target)
    if ssh is None:
        return 1
    label = "dc_backup" if target == "backup" else "app"

    if ensure_ssh_known_host_for_docker_host(f"ssh://{ssh}") != 0:
        print(f"Could not register SSH host key for {ssh!r}.", file=sys.stderr)
        return 1

    status = probe_host_logging(ssh)
    if status is None:
        return 1

    if not apply:
        return 0 if report_host_logging(label, status, env_name=env_name) else 1

    if status.daemon_ok:
        print(f"remote {label}: {DAEMON_JSON_PATH} already sets {LOG_DRIVER} {LOG_OPTS}.")
        return 0 if report_host_logging(label, status, env_name=env_name) else 1

    body = render_daemon_json(merge_daemon_config(status.daemon_json))
    print(f"--- {ssh}:{DAEMON_JSON_PATH} (new) ---", file=sys.stderr)
    print(body, end="", file=sys.stderr)
    if dry_run:
        print(
            "(dry-run) Would validate and install the file above, then restart docker "
            "(every container on the host restarts).",
            file=sys.stderr,
        )
        return 0

    if not yes:
        if not sys.stdin.isatty():
            print(
                "Refusing to restart docker without a TTY. Pass --yes for non-interactive use.",
                file=sys.stderr,
            )
            return 1
        print(
            f"WARNING: this restarts docker on {ssh}; every container on the host stops and "
            "starts again (a brief outage).",
            file=sys.stderr,
        )
        if not confirm_by_typing_env_name(env_name):
            print("host logging --apply cancelled.", file=sys.stderr)
            return 1

    rc = _write_daemon_json(ssh, body, had_file=status.daemon_json is not None)
    if rc != 0:
        print(
            f"Could not install {DAEMON_JSON_PATH} on {ssh}; the existing file was left as it was.",
            file=sys.stderr,
        )
        return rc
    restart = remote_run_capture(ssh, "sudo systemctl restart docker")
    if restart.returncode != 0:
        print(
            f"systemctl restart docker failed on {ssh}: {restart.stderr.strip()}",
            file=sys.stderr,
        )
        return restart.returncode

    after = probe_host_logging(ssh)
    if after is None:
        return 1
    report_host_logging(label, after, env_name=env_name)
    # Applying succeeded even if containers still need recreating; that is reported above.
    return 0 if after.daemon_ok else 1
