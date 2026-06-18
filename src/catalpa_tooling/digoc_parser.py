"""argparse tree for ``dk digoc``."""

from __future__ import annotations

import argparse

from catalpa_tooling.cloud_config.render import DEFAULT_TIMEZONE
from catalpa_tooling.cli.completion import attach_choices_completer
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.remote_deploy import list_deploy_env_names

PROG = "dk digoc"


def attach_digoc_subcommands(parser: argparse.ArgumentParser, config: ProjectConfig | None = None) -> None:
    """Attach ``digoc`` subcommands to an existing ``dk digoc`` parser node."""
    deploy_env_choices: list[str] = []
    if config is not None:
        deploy_env_choices = list_deploy_env_names(config.deploy_envs_dir)

    sub = parser.add_subparsers(dest="digoc_command", required=True)

    auth = sub.add_parser("auth", help="Manage doctl authentication contexts.")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    p_auth_init = auth_sub.add_parser("init", help="Initialize API token (interactive or --access-token).")
    p_auth_init.add_argument("--context", help="Named authentication context (default: default)")
    p_auth_init.add_argument(
        "-t",
        "--access-token",
        dest="access_token",
        metavar="TOKEN",
        help="API token (non-interactive; forwarded to host doctl)",
    )
    p_auth_init.set_defaults(handler="auth-init")

    p_auth_list = auth_sub.add_parser("list", help="List authentication contexts.")
    p_auth_list.add_argument("--context", help="Unused; listed for symmetry with other subcommands")
    p_auth_list.set_defaults(handler="auth-list")

    auth_sub.add_parser("remove", help="Remove a stored context (pass through to host doctl).").set_defaults(
        handler="auth-forward", auth_forward_sub="remove"
    )
    auth_sub.add_parser("switch", help="Switch active context (pass through to host doctl).").set_defaults(
        handler="auth-forward", auth_forward_sub="switch"
    )

    projects = sub.add_parser("projects", help="DigitalOcean projects.")
    projects_sub = projects.add_subparsers(dest="projects_command", required=True)
    p_projects_list = projects_sub.add_parser("list", help="List DigitalOcean projects.")
    p_projects_list.add_argument("--context", help="Authentication context")
    p_projects_list.add_argument(
        "--format",
        default="ID,Name,Purpose",
        help="Columns for host doctl output (default: ID,Name,Purpose)",
    )
    p_projects_list.set_defaults(handler="projects-list")

    cloud = sub.add_parser("cloud-config", help="Droplet bootstrap cloud-config.")
    cloud_sub = cloud.add_subparsers(dest="cloud_config_command", required=True)
    p_cc_print = cloud_sub.add_parser(
        "print",
        help="Print rendered droplet bootstrap cloud-config (#cloud-config).",
    )
    p_cc_print.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"IANA timezone (default: {DEFAULT_TIMEZONE})",
    )
    p_cc_print.set_defaults(handler="cloud-config-print")

    droplets = sub.add_parser("droplets", help="DigitalOcean droplets.")
    droplets_sub = droplets.add_subparsers(dest="droplets_command", required=True)

    p_d_list = droplets_sub.add_parser("list", help="List droplets assigned to a DigitalOcean project.")
    p_d_list.add_argument(
        "--project",
        metavar="NAME|UUID",
        help="Project name or UUID (default: digitalocean.* in tooling.yaml)",
    )
    p_d_list.add_argument("--context", help="Authentication context")
    p_d_list.add_argument(
        "--format",
        default=",".join(("ID", "Name", "PublicIPv4", "PrivateIPv4", "Region", "Status")),
        help="Comma-separated columns for text output",
    )
    p_d_list.add_argument("--json", action="store_true", help="Emit JSON (droplet objects)")
    p_d_list.set_defaults(handler="droplets-list")

    p_d_create = droplets_sub.add_parser(
        "create",
        help="Create a droplet with Docker CE, UFW, unattended upgrades, and SSH hardening.",
    )
    p_d_create.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Droplet hostname (default with --for-env: digitalocean.droplet_name or {project}-{env})",
    )
    for_env = p_d_create.add_argument(
        "--for-env",
        metavar="ENV",
        help="Use resolved droplet name for ENV when NAME is omitted",
    )
    if deploy_env_choices:
        attach_choices_completer(for_env, deploy_env_choices)
    p_d_create.add_argument(
        "--project",
        metavar="NAME|UUID",
        help="Project name or UUID (default: digitalocean.* in tooling.yaml)",
    )
    p_d_create.add_argument("--size", help="Droplet size slug (e.g. s-2vcpu-4gb)")
    p_d_create.add_argument(
        "--image",
        help="Image slug (default: ubuntu-24-04-x64 or digitalocean.image)",
    )
    p_d_create.add_argument("--region", help="Region slug (e.g. sgp1)")
    p_d_create.add_argument(
        "--ssh-key",
        action="append",
        dest="ssh_keys",
        default=[],
        metavar="ID|FINGERPRINT",
        help="SSH key ID or fingerprint (repeatable; default: all keys from host doctl)",
    )
    p_d_create.add_argument(
        "--timezone",
        help=f"IANA timezone (default: {DEFAULT_TIMEZONE} or digitalocean.timezone)",
    )
    p_d_create.add_argument("--context", help="Authentication context")
    p_d_create.add_argument("--wait", action="store_true", help="Wait until the droplet is active")
    p_d_create.add_argument(
        "--dry-run",
        action="store_true",
        help="Print cloud-config and host doctl command without creating",
    )
    p_d_create.add_argument(
        "--no-monitoring",
        action="store_true",
        help="Omit --enable-monitoring (default: install DO metrics agent)",
    )
    p_d_create.set_defaults(handler="droplets-create")

    p_d_suggest = droplets_sub.add_parser(
        "suggest-env",
        help="Deprecated; use dk <env> host.",
    )
    env_arg = p_d_suggest.add_argument("env", help="Deploy environment name (docker/envs/<env>/)")
    if deploy_env_choices:
        attach_choices_completer(env_arg, deploy_env_choices)
    p_d_suggest.add_argument(
        "--write",
        action="store_true",
        help="Write docker_host to info.yaml",
    )
    p_d_suggest.set_defaults(handler="droplets-suggest-env")


def build_digoc_parser(config: ProjectConfig | None = None) -> argparse.ArgumentParser:
    """Standalone ``dk digoc`` parser (for tests and completion)."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "DigitalOcean helpers for dk deploy (requires the official doctl binary on PATH or DOCTL_BIN)."
        ),
    )
    attach_digoc_subcommands(parser, config)
    return parser
