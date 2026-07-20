"""argparse subtree for ``dk <env> …`` commands."""

from __future__ import annotations

import argparse

from catalpa_tooling.cli.completion import attach_choices_completer
from catalpa_tooling.cli.parents import build_env_flags_parent
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.remote_deploy import list_dk_env_names
from catalpa_tooling.zabbix_systemd import DEFAULT_IMAGE, UNIT_NAME

from catalpa_tooling.cli.dk_argv import SPECIAL_ENV_COMMANDS

COMMON_COMPOSE_VERBS: tuple[str, ...] = (
    "up",
    "down",
    "ps",
    "logs",
    "exec",
    "run",
    "pull",
    "build",
    "config",
    "restart",
    "stop",
    "start",
)

COMMON_DOCKER_VERBS: tuple[str, ...] = (
    "run",
    "ps",
    "images",
    "volume",
    "network",
    "exec",
    "logs",
    "inspect",
    "pull",
    "rm",
    "stop",
    "start",
)

RESERVED_DK_TOP_COMMANDS = frozenset(
    {"build", "push", "transfer", "digoc", "fetch", "clean-images", "proxy", "cut-release"}
)


def _attach_zabbix_commands(cmd_sub: argparse._SubParsersAction, env_name: str) -> None:
    prog = f"dk {env_name} zabbix"
    p_zabbix = cmd_sub.add_parser("zabbix", help="Install or control Zabbix Agent 2 via systemd.")
    p_zabbix.add_argument(
        "--target",
        choices=("app", "backup"),
        default="app",
        help=(
            "Where to install/manage the agent: app docker_host (default) or "
            "dc_backup_docker_host (Garage/backup machine)."
        ),
    )
    zabbix_sub = p_zabbix.add_subparsers(dest="zabbix_command", required=True)

    p_install = zabbix_sub.add_parser(
        "install",
        help=f"Write {UNIT_NAME} and env files under /etc/indmo; systemctl daemon-reload.",
    )
    p_install.add_argument("--image", default=DEFAULT_IMAGE, help="Agent image for pull/run.")
    p_install.add_argument("--server", metavar="HOST", default=None, help="Set ZBX_SERVER_HOST.")
    p_install.add_argument(
        "--hostname",
        metavar="NAME",
        default=None,
        help=(
            "Set ZBX_HOSTNAME (must match the host in Zabbix). "
            "For --target backup: required via flag or zbx_hostname_backup (not site_origin)."
        ),
    )
    p_install.add_argument(
        "--active-allow",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="Set or clear ZBX_ACTIVE_ALLOW.",
    )
    p_install.add_argument("--docker-group-gid", type=int, default=None, metavar="GID")
    p_install.add_argument("--dry-run", action="store_true")
    p_install.add_argument("--force", action="store_true")

    for name in ("enable", "disable", "restart"):
        p = zabbix_sub.add_parser(name, help=f"systemctl {name} {UNIT_NAME}.")
        p.add_argument("--dry-run", action="store_true")

    p_logs = zabbix_sub.add_parser("logs", help=f"journalctl for {UNIT_NAME}.")
    p_logs.add_argument("-n", "--lines", type=int, default=100, metavar="N")
    p_logs.add_argument("-f", "--follow", action="store_true")


def _attach_files_commands(cmd_sub: argparse._SubParsersAction, env_name: str) -> None:
    def _build_files_parser(name: str, *, dest: str) -> argparse.ArgumentParser:
        p = cmd_sub.add_parser(name, help="restic backups and host media → volume.")
        sub = p.add_subparsers(dest=dest, required=True)

        p_push = sub.add_parser("push", help="Rsync host media into django_media volume.")
        p_push.add_argument("--source", "-s", default=None, help="Host directory (default: native.fetch_media.dest).")
        p_push.add_argument("--method", choices=("rsync", "tar"), default="rsync")
        p_push.add_argument("--image", default="alpine:3.21")

        p_install = sub.add_parser("install-systemd", help="Install restic systemd units on deploy host.")
        p_install.add_argument("--dry-run", action="store_true")
        p_install.add_argument("--enable", action="store_true")

        for sub_name, help_text in (
            ("init", "Initialize restic repository."),
            ("backup", "Run restic backup."),
            ("snapshots", "List restic snapshots."),
            ("check", "Run restic check."),
            ("stats", "Run restic stats."),
        ):
            sub.add_parser(sub_name, help=help_text)

        p_restore = sub.add_parser("restore", help="Restore restic snapshot (default: latest).")
        p_restore.add_argument(
            "snapshot",
            nargs="?",
            default="latest",
            help="Snapshot ID (default: latest).",
        )
        return p

    _build_files_parser("files", dest="files_command")
    _build_files_parser("bkp_files", dest="bkp_files_command")


def _attach_db_commands(cmd_sub: argparse._SubParsersAction, env_name: str) -> None:
    def _build_db_parser(name: str, *, dest: str) -> argparse.ArgumentParser:
        p = cmd_sub.add_parser(name, help="pgBackRest backups and pg dump/restore.")
        sub = p.add_subparsers(dest=dest, required=True)

        p_init = sub.add_parser("init", help="Initialize pgBackRest volumes and stanza.")
        p_init.add_argument("--install-systemd", dest="install_systemd", action="store_true")
        p_init.add_argument("--dry-run", action="store_true")
        p_init.add_argument("--enable", action="store_true")

        p_configure = sub.add_parser("configure", help="Materialize pgBackRest config into volume.")
        configure_sub = p_configure.add_subparsers(dest="configure_mode")
        configure_sub.add_parser("verify", help="Run offline verify + online check.")
        configure_sub.add_parser("stanza-create", help="Create stanza (starts db if needed).")

        p_install = sub.add_parser("install-systemd", help="Install pgBackRest systemd units.")
        p_install.add_argument("--dry-run", action="store_true")
        p_install.add_argument("--enable", action="store_true")

        for sub_name in ("info", "check", "version"):
            sub.add_parser(sub_name, help=f"pgBackRest {sub_name}.")

        p_backup = sub.add_parser("backup", help="Online pgBackRest backup.")
        backup_type = p_backup.add_argument(
            "backup_type",
            choices=("full", "incr", "diff"),
            help="Backup type.",
        )
        attach_choices_completer(backup_type, ("full", "incr", "diff"))

        p_pgdump = sub.add_parser("pgdump", help="pg_dump custom-format archive to stdout.")
        p_pgdump.add_argument("pg_dump_args", nargs=argparse.REMAINDER)

        p_pgrestore = sub.add_parser("pgrestore", help="pg_restore into app database.")
        p_pgrestore.add_argument("--file", dest="archive_file", default=None, metavar="PATH")
        p_pgrestore.add_argument("pg_restore_args", nargs=argparse.REMAINDER)

        p_restore = sub.add_parser("restore", help="Offline pgBackRest restore.")
        p_restore.add_argument(
            "--dumps",
            action="store_true",
            dest="restore_from_dumps",
            help="Restore from local custom-format dumps only (never pgBackRest; never auto-fetch).",
        )
        p_restore.add_argument(
            "--dry-run",
            action="store_true",
            dest="restore_dry_run",
            help="Print restore plan (stanza, repo path, compose command) without running.",
        )
        p_restore.add_argument("pgbackrest_restore_args", nargs=argparse.REMAINDER)
        return p

    _build_db_parser("db", dest="db_command")
    _build_db_parser("bkp_db", dest="bkp_db_command")


def _attach_env_command_parsers(
    cmd_sub: argparse._SubParsersAction,
    config: ProjectConfig,
    env_name: str,
) -> None:
    p_info = cmd_sub.add_parser("info", help="Print or edit docker/envs/<env>/info.yaml.")
    p_info.add_argument("-e", "--edit", action="store_true", help="Edit info.yaml in $EDITOR.")

    cmd_sub.add_parser("secrets", help="Edit credentials.yaml with SOPS.")

    p_dc = cmd_sub.add_parser(
        "dc-backup",
        help="Closed-DC Garage backup: TLS certs and Garage+Caddy stack.",
    )
    dc_sub = p_dc.add_subparsers(dest="dc_backup_command", required=True)

    p_tls = dc_sub.add_parser("tls", help="Issue / install / status for private CA + server cert.")
    tls_sub = p_tls.add_subparsers(dest="dc_backup_tls_command", required=True)
    p_issue = tls_sub.add_parser(
        "issue",
        help="Generate CA + server cert (openssl) and write SOPS dc-backup-tls.yaml.",
    )
    p_issue.add_argument(
        "--ip",
        dest="ips",
        action="append",
        default=[],
        metavar="IP",
        help="IPv4 SAN (repeatable; at least one required).",
    )
    p_issue.add_argument(
        "--dns",
        dest="dns_names",
        action="append",
        default=[],
        metavar="NAME",
        help="DNS SAN (repeatable).",
    )
    p_issue.add_argument(
        "--days",
        type=int,
        default=825,
        metavar="N",
        help="Certificate validity in days (default: 825).",
    )
    p_issue.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing dc-backup-tls.yaml.",
    )
    tls_sub.add_parser(
        "install",
        help="Install PEMs: CA+server on dc_backup_docker_host; CA on docker_host.",
    )
    p_tls_status = tls_sub.add_parser(
        "status",
        help="Show dc-backup-tls.yaml presence and SANs (no secret dump).",
    )
    p_tls_status.add_argument(
        "--check-remote",
        action="store_true",
        help="Also probe installed paths on dc_backup_docker_host and docker_host.",
    )

    p_boot = dc_sub.add_parser(
        "bootstrap",
        help="Generate Garage rpc/admin secrets into SOPS dc-backup.yaml.",
    )
    p_boot.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing dc-backup.yaml.",
    )

    p_install = dc_sub.add_parser(
        "install",
        help="Install Garage+Caddy compose, toml, and Caddyfile on dc_backup_docker_host.",
    )
    p_install.add_argument(
        "--up",
        action="store_true",
        help="Run docker compose up -d after installing files.",
    )

    p_dc_status = dc_sub.add_parser(
        "status",
        help="Show dc-backup.yaml presence and optional remote stack paths.",
    )
    p_dc_status.add_argument(
        "--check-remote",
        action="store_true",
        help="Probe compose/toml/Caddy/TLS paths on dc_backup_docker_host.",
    )

    p_provision = dc_sub.add_parser(
        "provision",
        help="Create Garage bucket/key and write pgbr_s3_write_* / restic_write_* credentials.",
    )
    p_provision.add_argument(
        "--print-only",
        action="store_true",
        help="Create/print credentials YAML fragment; do not write SOPS credentials.yaml.",
    )
    p_provision.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing WRITE credentials (required to replace Spaces with Garage).",
    )
    p_provision.add_argument(
        "--dry-run",
        action="store_true",
        dest="dc_backup_provision_dry_run",
        help="Print what would run / which keys would be set; no Garage mutations, no SOPS write.",
    )
    p_provision.add_argument(
        "--bucket",
        metavar="NAME",
        help="Garage bucket name (default: {project}-backups).",
    )
    p_provision.add_argument(
        "--key-name",
        metavar="NAME",
        help="Garage key name (default: {project}-{env}-backup).",
    )
    p_provision.add_argument(
        "--endpoint",
        metavar="HOST",
        help="S3 endpoint host/IP (default: TLS SAN IP, else backup host).",
    )
    p_provision.add_argument(
        "--pgbr-repo-path",
        metavar="PATH",
        help="pgBackRest repo path (default: /{project}/{env}/pgbackrest).",
    )
    p_provision.add_argument(
        "--restic-prefix",
        metavar="PATH",
        help="restic path prefix inside the bucket (default: {project}-{env}-media).",
    )
    p_provision.add_argument(
        "--capacity",
        metavar="SIZE",
        help="Garage layout capacity if no role assigned yet (default: 300G).",
    )

    p_offsite = dc_sub.add_parser(
        "offsite",
        help="rclone copy Garage bucket → external S3 (daily OOH on dc_backup_docker_host).",
    )
    offsite_sub = p_offsite.add_subparsers(dest="dc_backup_offsite_command", required=True)
    p_off_install = offsite_sub.add_parser(
        "install",
        help="Install rclone script, env file, and systemd timer on dc_backup_docker_host.",
    )
    p_off_install.add_argument(
        "--enable",
        action="store_true",
        help="systemctl enable --now the offsite timer after install.",
    )
    p_off_install.add_argument(
        "--dry-run",
        action="store_true",
        dest="dc_backup_offsite_dry_run",
        help="Print what would be installed; no remote mutations.",
    )
    p_off_run = offsite_sub.add_parser(
        "run",
        help="One-shot: refresh env and systemctl start the offsite oneshot service.",
    )
    p_off_run.add_argument(
        "--dry-run",
        action="store_true",
        dest="dc_backup_offsite_dry_run",
        help="Print what would run; no remote mutations.",
    )
    offsite_sub.add_parser(
        "status",
        help="Show timer/service ActiveState on dc_backup_docker_host.",
    )

    p_host = cmd_sub.add_parser("host", help="Verify droplet / print or patch docker_host.")
    host_sub = p_host.add_subparsers(dest="host_command", required=False)
    p_host_create = host_sub.add_parser("create", help="Provision a new droplet.")
    p_host_create.add_argument("host_create_args", nargs=argparse.REMAINDER)
    p_host.add_argument("--write", action="store_true", help="Write docker_host from droplet public IPv4.")
    p_host.add_argument(
        "--sync-dns",
        action="store_true",
        help="Create or update DigitalOcean A records for site_origin hostnames.",
    )
    p_host.add_argument(
        "--check-remote",
        action="store_true",
        help="SSH probe docker_host (and dc_backup_docker_host): reachability + timedatectl.",
    )

    _attach_zabbix_commands(cmd_sub, env_name)

    cmd_sub.add_parser("ensure_volumes", help="Ensure external stack volumes exist.")

    p_storage = cmd_sub.add_parser(
        "storage",
        help="Host storage paths and Docker volume binds (optional DO block volumes).",
    )
    storage_sub = p_storage.add_subparsers(dest="storage_command", required=True)
    storage_sub.add_parser(
        "ensure",
        help="Verify host paths, optional DO volumes, and Docker named-volume binds.",
    )

    p_trust = cmd_sub.add_parser(
        "trust-caddy-cert",
        help="Trust Caddy local CA (global local proxy or stack proxy service).",
    )

    p_manage = cmd_sub.add_parser("manage", help="docker compose exec web ./manage.py …")
    p_manage.add_argument("manage_args", nargs=argparse.REMAINDER)

    p_pull = cmd_sub.add_parser("pull_media", help="Copy django_media volume to local directory.")
    p_pull.add_argument("--target", "-t", default=None, metavar="DIR")
    p_pull.add_argument("--image", default="alpine:3.21")

    cmd_sub.add_parser("wipe", help="Alias: docker compose down -v (with confirmation).")

    _attach_files_commands(cmd_sub, env_name)
    _attach_db_commands(cmd_sub, env_name)

    p_compose = cmd_sub.add_parser(
        "compose",
        help="Passthrough to docker compose (completion-friendly; e.g. dk full compose up -d).",
    )
    compose_verb = p_compose.add_argument(
        "compose_argv",
        nargs=argparse.REMAINDER,
        help="Docker compose arguments (default when omitted: up -d).",
    )
    attach_choices_completer(compose_verb, COMMON_COMPOSE_VERBS)

    p_docker = cmd_sub.add_parser(
        "docker",
        help="Passthrough to docker (same DOCKER_HOST/env as compose; e.g. dk prod docker volume ls).",
    )
    docker_verb = p_docker.add_argument(
        "docker_argv",
        nargs=argparse.REMAINDER,
        help="Docker CLI arguments (empty runs bare docker for its own help).",
    )
    attach_choices_completer(docker_verb, COMMON_DOCKER_VERBS)


def attach_env_subparsers(
    subparsers: argparse._SubParsersAction,
    config: ProjectConfig,
) -> None:
    """Register one subparser per deploy environment under ``dk``."""
    env_flags = build_env_flags_parent()
    env_names = list_dk_env_names(config)
    envs_dir = config.paths.deploy.envs_dir

    for name in env_names:
        if name in RESERVED_DK_TOP_COMMANDS:
            continue
        p = subparsers.add_parser(
            name,
            parents=[env_flags],
            help=f"Environment {envs_dir}/{name}/info.yaml.",
        )
        p.set_defaults(env_name=name, dk_command=name)
        cmd_sub = p.add_subparsers(dest="env_command", required=False)
        _attach_env_command_parsers(cmd_sub, config, name)
        p.add_argument(
            "implicit_compose_argv",
            nargs=argparse.REMAINDER,
            default=[],
            help=argparse.SUPPRESS,
        )
