"""Zabbix on an FCOS host, rendered when tooling.yaml has ``ops.zabbix``.

Three parts, tested on fcos-rootless (catalpa-site#1006):

1. **Host metrics.** Zabbix Agent 2 is a root-owned *system* quadlet, so the account can't stop
   its own monitoring. The agent itself runs as the image's zabbix user (uid 1997) with no
   capabilities and active checks only. The Ubuntu install runs it ``--privileged`` as root
   with the Docker socket.
2. **Containers.** ``podman-ro-proxy`` (HAProxy) holds the account's Podman socket and passes on
   only read requests to the endpoints the Docker plugin uses.
3. **Backups.** The agent reads the status files ``backup-status.sh`` writes, with the same item
   keys (``pgbackrest.info``, ``restic.snapshots``) as the Ubuntu install.

Settings come from the same keys as ``dk <env> zabbix install``: ``zbx_*`` at the top of
info.yaml and ``ZBX_*`` in ``env:``. ``zbx_tlspsk`` / ``zbx_tlspskidentity`` in credentials.yaml
become secrets of the system (root's) Podman.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from catalpa_tooling.fcos.quadlets import has_secret, secret_name
from catalpa_tooling.fcos.settings import FcosSettings

#: The zabbix group in the agent image: the only group allowed into the read-only socket's and
#: status files' directories.
ZABBIX_GID = 1995
PROXY_IMAGE = "docker.io/library/haproxy:3.0-alpine"
_SECRET_RE = r"@@dk-secret:([A-Za-z_][A-Za-z0-9_]*)@@"


@dataclass
class MonitoringFiles:
    files: dict[str, str] = field(default_factory=dict)
    install: list[dict] = field(default_factory=list)
    system_secrets: dict[str, str] = field(default_factory=dict)
    tmpfiles: list[str] = field(default_factory=list)


def monitor_dir(project: str) -> str:
    return f"/var/lib/{project}-monitor"


def ro_socket_dir(project: str) -> str:
    return f"/run/{project}-podman-ro"


def monitoring_files(
    *,
    settings: FcosSettings,
    header: str,
    project: str,
    config_dir: str,
    userparams_file: str,
    zbx_env: dict[str, str],
    hostname: str,
    backups: bool,
) -> MonitoringFiles:
    import re

    from catalpa_tooling.zabbix_systemd import (
        DEFAULT_ZBX_SERVER_HOST,
        ZBX_ENV_RESERVED_FOR_MERGE,
        ensure_zbx_metadata_tokens,
    )

    out = MonitoringFiles()
    ro_dir = ro_socket_dir(project)

    def add(path: str, text: str, dest: str, owner: str = "root") -> None:
        out.files[path] = header + text
        out.install.append({"file": path, "dest": dest, "owner": owner, "mode": "0644"})

    keys = {
        "ZBX_SERVER_HOST": zbx_env.get("ZBX_SERVER_HOST") or DEFAULT_ZBX_SERVER_HOST,
        "ZBX_HOSTNAME": zbx_env.get("ZBX_HOSTNAME") or hostname,
        # Active checks only: the agent connects out to the server and listens on nothing.
        "ZBX_ACTIVE_ALLOW": "true",
        "ZBX_PASSIVE_ALLOW": "false",
    }
    keys.update(
        {k: v for k, v in zbx_env.items() if k.startswith("ZBX_") and k not in ZBX_ENV_RESERVED_FOR_MERGE}
    )
    if keys.get("ZBX_TLSPSK") and keys.get("ZBX_TLSPSKIDENTITY") and not keys.get("ZBX_TLSCONNECT"):
        keys["ZBX_TLSCONNECT"] = "psk"
    ensure_zbx_metadata_tokens(keys, target="app")
    env_lines = []
    for key, value in sorted(keys.items()):
        m = re.fullmatch(_SECRET_RE, value)
        if m:
            out.system_secrets[secret_name(m.group(1))] = m.group(1)
            env_lines.append(f"Secret={secret_name(m.group(1))},type=env,target={key}")
        elif has_secret(value):
            raise ValueError(f"{key}: a credential inside a longer value can't be a secret")
        else:
            env_lines.append(f"Environment={key}={value}")

    agent_volumes = [
        "# Disk items (vfs.fs.*) statfs the host's filesystems under /host/root. Read-only, and",
        "# rslave so the data volume shows up if it's mounted after the agent starts.",
        "Volume=/:/host/root:ro,rslave",
        "# The read-only Podman API (podman-ro-proxy), and the Docker plugin pointed at it.",
        f"Volume={ro_dir}:/run/podman-ro:ro",
        f"Volume={config_dir}/zabbix-docker.conf:/etc/zabbix/zabbix_agent2.d/plugins.d/docker.conf:ro",
    ]
    if backups:
        agent_volumes += [
            "# Backup status files, and the UserParameters that read them.",
            f"Volume={monitor_dir(project)}:{monitor_dir(project)}:ro",
            f"Volume={config_dir}/zabbix-userparams.conf:/etc/zabbix/zabbix_agent2.d/plugins.d/{userparams_file}:ro",
        ]
    add(
        "quadlets/zabbix-agent2.container",
        "# A root-owned system quadlet, so the account can't stop its own monitoring. The agent\n"
        "# is NOT root: the image's zabbix user (uid 1997), no capabilities, no way to gain any,\n"
        "# no container socket.\n"
        "[Unit]\n"
        "Description=Zabbix Agent 2\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Container]\n"
        "ContainerName=zabbix-agent2\n"
        f"Image={settings.zabbix_image}\n"
        "AutoUpdate=registry\n"
        "# Host network and PID namespace: network and process items see the host's.\n"
        "Network=host\n"
        "PodmanArgs=--pid=host\n"
        + "\n".join(agent_volumes)
        + "\n"
        "# SELinux would block even stat-ing most host paths; file permissions still apply.\n"
        "SecurityLabelDisable=true\n"
        "DropCapability=all\n"
        "NoNewPrivileges=true\n" + "\n".join(env_lines) + "\n"
        "\n"
        "[Service]\n"
        "Restart=always\n"
        "TimeoutStartSec=300\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n",
        "/etc/containers/systemd/zabbix-agent2.container",
    )
    add(
        "quadlets/podman-ro-proxy.container",
        "# Read-only Podman API for the Zabbix agent. It holds the account's full Podman socket, so\n"
        "# it runs as the account (no UIDMap), with no network, a read-only root filesystem and no\n"
        "# capabilities. Its config is root-owned, so the account can't widen what it allows.\n"
        "[Unit]\n"
        "Description=Read-only Podman API for monitoring\n"
        "Requires=podman.socket\n"
        "After=podman.socket\n"
        "\n"
        "[Container]\n"
        "ContainerName=podman-ro-proxy\n"
        f"Image={PROXY_IMAGE}\n"
        "AutoUpdate=registry\n"
        "Network=none\n"
        "User=0\n"
        f"Volume={config_dir}/podman-ro-proxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro\n"
        "# The socket's directory, not the file, so a restarted podman.socket is still seen.\n"
        "Volume=%t/podman:/run/podman\n"
        f"Volume={ro_dir}:/run/podman-ro\n"
        "# SELinux doesn't let a container connect to Podman's own socket.\n"
        "SecurityLabelDisable=true\n"
        "DropCapability=all\n"
        "NoNewPrivileges=true\n"
        "ReadOnly=true\n"
        "\n"
        "[Service]\n"
        "Restart=always\n"
        "TimeoutStartSec=300\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n",
        f"{settings.quadlet_dir}/podman-ro-proxy.container",
    )
    add(
        "monitoring/podman-ro-proxy.cfg",
        "# Only GET/HEAD to the endpoints the Docker plugin uses; anything that changes state or\n"
        "# could leak more than state and resource use (logs, archive, exec, secrets) gets 403.\n"
        "global\n"
        "    log stdout format raw local0 info\n"
        "\n"
        "defaults\n"
        "    mode http\n"
        "    log global\n"
        "    option httplog\n"
        "    timeout connect 5s\n"
        "    timeout client 30s\n"
        "    timeout server 30s\n"
        "\n"
        "frontend podman_ro\n"
        f"    # {ro_dir} on the host: 0750 with group {ZABBIX_GID}, the agent's.\n"
        "    bind unix@/run/podman-ro/podman.sock mode 666\n"
        "    http-request deny deny_status 403 unless { method GET HEAD }\n"
        "    http-request deny deny_status 403 unless { path -m reg "
        "^(/v[0-9.]+)?/(_ping|version|info|system/df|images/json|containers/json|"
        "containers/[A-Za-z0-9_.-]+/(json|stats))$ }\n"
        "    default_backend podman\n"
        "\n"
        "backend podman\n"
        "    server podman unix@/run/podman/podman.sock\n",
        f"{config_dir}/podman-ro-proxy.cfg",
    )
    add(
        "monitoring/zabbix-docker.conf",
        "Plugins.Docker.Endpoint=unix:///run/podman-ro/podman.sock\n",
        f"{config_dir}/zabbix-docker.conf",
    )
    if backups:
        d = monitor_dir(project)
        add(
            "monitoring/zabbix-userparams.conf",
            "# The same item keys as dk's userparams on Ubuntu, read from backup-status.sh's files.\n"
            f"UserParameter=pgbackrest.info,cat {d}/pgbackrest-info.json\n"
            f"UserParameter=restic.snapshots,cat {d}/restic-snapshots.json\n",
            f"{config_dir}/zabbix-userparams.conf",
        )
    out.tmpfiles.append(f"d {ro_dir} 0750 {settings.user} {ZABBIX_GID} -")
    return out
