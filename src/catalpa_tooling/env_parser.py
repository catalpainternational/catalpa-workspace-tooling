"""argparse subtree for ``dk <env> …`` commands."""

from __future__ import annotations

import argparse

from catalpa_tooling.cli.completion import attach_choices_completer
from catalpa_tooling.cli.parents import build_env_flags_parent
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.remote_deploy import list_deploy_env_names
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

RESERVED_DK_TOP_COMMANDS = frozenset({"build", "push", "transfer", "digoc"})


def _attach_zabbix_commands(cmd_sub: argparse._SubParsersAction, env_name: str) -> None:
    prog = f"dk {env_name} zabbix"
    p_zabbix = cmd_sub.add_parser("zabbix", help="Install or control Zabbix Agent 2 via systemd.")
    zabbix_sub = p_zabbix.add_subparsers(dest="zabbix_command", required=True)

    p_install = zabbix_sub.add_parser(
        "install",
        help=f"Write {UNIT_NAME} and env files under /etc/indmo; systemctl daemon-reload.",
    )
    p_install.add_argument("--image", default=DEFAULT_IMAGE, help="Agent image for pull/run.")
    p_install.add_argument("--server", metavar="HOST", default=None, help="Set ZBX_SERVER_HOST.")
    p_install.add_argument("--hostname", metavar="NAME", default=None, help="Set ZBX_HOSTNAME.")
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


def _attach_bkp_files_commands(cmd_sub: argparse._SubParsersAction, env_name: str) -> None:
    p_bkp = cmd_sub.add_parser("bkp_files", help="restic backups and host media → volume.")
    bkp_sub = p_bkp.add_subparsers(dest="bkp_files_command", required=True)

    p_push = bkp_sub.add_parser("push", help="Rsync host media into django_media volume.")
    p_push.add_argument("--source", "-s", default=None, help="Host directory (default: dev.fetch_media.dest).")
    p_push.add_argument("--method", choices=("rsync", "tar"), default="rsync")
    p_push.add_argument("--image", default="alpine:3.21")

    p_install = bkp_sub.add_parser("install-systemd", help="Install restic systemd units on deploy host.")
    p_install.add_argument("--dry-run", action="store_true")
    p_install.add_argument("--enable", action="store_true")

    for name, help_text in (
        ("init", "Initialize restic repository."),
        ("backup", "Run restic backup."),
        ("snapshots", "List restic snapshots."),
        ("check", "Run restic check."),
        ("stats", "Run restic stats."),
    ):
        bkp_sub.add_parser(name, help=help_text)

    p_restore = bkp_sub.add_parser("restore", help="Restore restic snapshot (default: latest).")
    p_restore.add_argument(
        "snapshot",
        nargs="?",
        default="latest",
        help="Snapshot ID (default: latest).",
    )


def _attach_bkp_db_commands(cmd_sub: argparse._SubParsersAction, env_name: str) -> None:
    p_bkp = cmd_sub.add_parser("bkp_db", help="pgBackRest backups and pg dump/restore.")
    bkp_sub = p_bkp.add_subparsers(dest="bkp_db_command", required=True)

    p_init = bkp_sub.add_parser("init", help="Initialize pgBackRest volumes and stanza.")
    p_init.add_argument("--install-systemd", dest="install_systemd", action="store_true")
    p_init.add_argument("--dry-run", action="store_true")
    p_init.add_argument("--enable", action="store_true")

    p_configure = bkp_sub.add_parser("configure", help="Materialize pgBackRest config into volume.")
    configure_sub = p_configure.add_subparsers(dest="configure_mode")
    configure_sub.add_parser("verify", help="Run offline verify + online check.")
    configure_sub.add_parser("stanza-create", help="Create stanza (starts db if needed).")

    p_install = bkp_sub.add_parser("install-systemd", help="Install pgBackRest systemd units.")
    p_install.add_argument("--dry-run", action="store_true")
    p_install.add_argument("--enable", action="store_true")

    for name in ("info", "check", "version"):
        bkp_sub.add_parser(name, help=f"pgBackRest {name}.")

    p_backup = bkp_sub.add_parser("backup", help="Online pgBackRest backup.")
    backup_type = p_backup.add_argument(
        "backup_type",
        choices=("full", "incr", "diff"),
        help="Backup type.",
    )
    attach_choices_completer(backup_type, ("full", "incr", "diff"))

    p_pgdump = bkp_sub.add_parser("pgdump", help="pg_dump custom-format archive to stdout.")
    p_pgdump.add_argument("pg_dump_args", nargs=argparse.REMAINDER)

    p_pgrestore = bkp_sub.add_parser("pgrestore", help="pg_restore into app database.")
    p_pgrestore.add_argument("--file", dest="archive_file", default=None, metavar="PATH")
    p_pgrestore.add_argument("pg_restore_args", nargs=argparse.REMAINDER)

    p_restore = bkp_sub.add_parser("restore", help="Offline pgBackRest restore.")
    p_restore.add_argument("pgbackrest_restore_args", nargs=argparse.REMAINDER)


def _attach_env_command_parsers(
    cmd_sub: argparse._SubParsersAction,
    config: ProjectConfig,
    env_name: str,
) -> None:
    p_info = cmd_sub.add_parser("info", help="Print or edit docker/envs/<env>/info.yaml.")
    p_info.add_argument("-e", "--edit", action="store_true", help="Edit info.yaml in $EDITOR.")

    cmd_sub.add_parser("secrets", help="Edit credentials.yaml with SOPS.")

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

    _attach_zabbix_commands(cmd_sub, env_name)

    cmd_sub.add_parser("ensure_volumes", help="Ensure external stack volumes exist.")

    p_trust = cmd_sub.add_parser(
        "trust-caddy-cert",
        help=f"macOS: trust Caddy local CA ({config.stack_service('proxy')} service).",
    )

    p_manage = cmd_sub.add_parser("manage", help="docker compose exec web ./manage.py …")
    p_manage.add_argument("manage_args", nargs=argparse.REMAINDER)

    p_pull = cmd_sub.add_parser("pull_media", help="Copy django_media volume to local directory.")
    p_pull.add_argument("--target", "-t", default=None, metavar="DIR")
    p_pull.add_argument("--image", default="alpine:3.21")

    cmd_sub.add_parser("wipe", help="Alias: docker compose down -v (with confirmation).")

    _attach_bkp_files_commands(cmd_sub, env_name)
    _attach_bkp_db_commands(cmd_sub, env_name)

    p_compose = cmd_sub.add_parser(
        "compose",
        help="Passthrough to docker compose (completion-friendly; e.g. dk local compose up -d).",
    )
    compose_verb = p_compose.add_argument(
        "compose_argv",
        nargs=argparse.REMAINDER,
        help="Docker compose arguments (default when omitted: up -d).",
    )
    attach_choices_completer(compose_verb, COMMON_COMPOSE_VERBS)


def attach_env_subparsers(
    subparsers: argparse._SubParsersAction,
    config: ProjectConfig,
) -> None:
    """Register one subparser per deploy environment under ``dk``."""
    env_flags = build_env_flags_parent()
    env_names = list_deploy_env_names(config.deploy_envs_dir)
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
