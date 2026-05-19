"""argparse entrypoint for doctl: DigitalOcean auth and project droplet listing."""

from __future__ import annotations

import argparse
import sys

from catalpa_tooling.cli_interrupt import run_cli
from catalpa_tooling.config import DigitalOceanConfig, ProjectConfig, ProjectConfigError
from catalpa_tooling.doctl_binary import ensure_doctl_available, run_doctl
from catalpa_tooling.cloud_config.render import DEFAULT_TIMEZONE, render_droplet_bootstrap
from catalpa_tooling.doctl_droplets import create_droplet
from catalpa_tooling.doctl_projects import list_project_droplets, resolve_project_id


def _cmd_auth_init(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="doctl auth init")
    p.add_argument(
        "--context",
        help="Named authentication context (default: default)",
    )
    p.add_argument(
        "-t",
        "--access-token",
        dest="access_token",
        metavar="TOKEN",
        help="API token (non-interactive; forwarded to doctl)",
    )
    ns, rest = p.parse_known_args(argv)
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")
    ensure_doctl_available()
    args = ["auth", "init"]
    if ns.context:
        args.extend(["--context", ns.context])
    if ns.access_token:
        args.extend(["--access-token", ns.access_token])
    elif sys.stdin.isatty():
        # Without --interactive, doctl re-validates the stored token and does not prompt.
        args.append("--interactive")
    else:
        print(
            "doctl auth init: not a TTY and no --access-token given.\n"
            "Run in a terminal, pass -t TOKEN, or clear a bad token with:\n"
            "  doctl auth remove --context default",
            file=sys.stderr,
        )
        return 1
    return run_doctl(args).returncode


def _cmd_auth_forward(subcommand: str, argv: list[str]) -> int:
    """Pass through to host ``doctl auth <subcommand>`` (remove, switch, …)."""
    ensure_doctl_available()
    return run_doctl(["auth", subcommand, *argv]).returncode


def _cmd_auth_list(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="doctl auth list")
    p.add_argument("--context", help="Unused; listed for symmetry with other subcommands")
    ns, rest = p.parse_known_args(argv)
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")
    ensure_doctl_available()
    return run_doctl(["auth", "list"], context=ns.context).returncode


def _cmd_projects_list(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="doctl projects list")
    p.add_argument("--context", help="Authentication context")
    p.add_argument(
        "--format",
        default="ID,Name,Purpose",
        help="Columns for doctl output (default: ID,Name,Purpose)",
    )
    ns, rest = p.parse_known_args(argv)
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")
    ensure_doctl_available()
    return run_doctl(
        ["projects", "list", "--format", ns.format],
        context=ns.context,
    ).returncode


def _cmd_cloud_config_print(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="doctl cloud-config print",
        description="Print rendered droplet bootstrap cloud-config (#cloud-config).",
    )
    p.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"IANA timezone (default: {DEFAULT_TIMEZONE})",
    )
    ns, rest = p.parse_known_args(argv)
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")
    print(render_droplet_bootstrap(timezone=ns.timezone), end="")
    return 0


def _load_do_config_for_droplets(
    *,
    project_flag: str | None,
) -> tuple[DigitalOceanConfig | None, str | None]:
    try:
        cfg = ProjectConfig.from_cwd()
        do_config = cfg.digitalocean
        return do_config, do_config.context if do_config else None
    except ProjectConfigError as e:
        if project_flag:
            return None, None
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e


def _cmd_droplets_create(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="doctl droplets create",
        description="Create a droplet with Docker CE, UFW, unattended upgrades, and SSH hardening.",
    )
    p.add_argument("name", help="Droplet hostname")
    p.add_argument(
        "--project",
        metavar="NAME|UUID",
        help="Project name or UUID (default: digitalocean.* in tooling.yaml)",
    )
    p.add_argument("--size", help="Droplet size slug (e.g. s-2vcpu-4gb)")
    p.add_argument(
        "--image",
        help="Image slug (default: ubuntu-24-04-x64 or digitalocean.image)",
    )
    p.add_argument("--region", help="Region slug (e.g. sgp1)")
    p.add_argument(
        "--ssh-key",
        action="append",
        dest="ssh_keys",
        default=[],
        metavar="ID|FINGERPRINT",
        help="SSH key ID or fingerprint (repeatable; default: all keys from doctl compute ssh-key list)",
    )
    p.add_argument(
        "--timezone",
        help=f"IANA timezone (default: {DEFAULT_TIMEZONE} or digitalocean.timezone)",
    )
    p.add_argument("--context", help="Authentication context")
    p.add_argument("--wait", action="store_true", help="Wait until the droplet is active")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print cloud-config and doctl command without creating",
    )
    ns, rest = p.parse_known_args(argv)
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")

    do_config, manifest_context = _load_do_config_for_droplets(project_flag=ns.project)
    context = ns.context or manifest_context
    ensure_doctl_available()
    project_id = resolve_project_id(ns.project, do_config=do_config, context=context)
    return create_droplet(
        ns.name,
        size=ns.size,
        image=ns.image,
        region=ns.region,
        project_id=project_id,
        ssh_keys=tuple(ns.ssh_keys),
        timezone=ns.timezone,
        context=context,
        wait=ns.wait,
        dry_run=ns.dry_run,
        do_config=do_config,
    )


def _cmd_droplets_list(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="doctl droplets list",
        description="List droplets assigned to a DigitalOcean project.",
    )
    p.add_argument(
        "--project",
        metavar="NAME|UUID",
        help="Project name or UUID (default: digitalocean.* in tooling.yaml)",
    )
    p.add_argument("--context", help="Authentication context")
    p.add_argument(
        "--format",
        default=",".join(("ID", "Name", "PublicIPv4", "PrivateIPv4", "Region", "Status")),
        help="Comma-separated columns for text output",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON (droplet objects)",
    )
    ns, rest = p.parse_known_args(argv)
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")

    ensure_doctl_available()
    try:
        cfg = ProjectConfig.from_cwd()
        do_config = cfg.digitalocean
        manifest_context = do_config.context if do_config else None
    except ProjectConfigError as e:
        if ns.project:
            do_config = None
            manifest_context = None
        else:
            print(str(e), file=sys.stderr)
            return 1

    context = ns.context or manifest_context
    project_id = resolve_project_id(ns.project, do_config=do_config, context=context)
    columns = tuple(c.strip() for c in ns.format.split(",") if c.strip())
    return list_project_droplets(
        project_id,
        context=context,
        columns=columns or None,
        as_json=ns.json,
    )


def _print_help() -> None:
    print(
        """usage: doctl [-h] {auth,cloud-config,projects,droplets} ...

DigitalOcean CLI wrapper (requires the official doctl binary on PATH).

  doctl auth init [--context NAME] [-t TOKEN]   Initialize API token (interactive)
  doctl auth list                    List authentication contexts
  doctl auth remove --context NAME   Remove a stored context
  doctl auth switch --context NAME   Switch active context
  doctl cloud-config print           Print droplet bootstrap cloud-config
  doctl projects list                List DigitalOcean projects
  doctl droplets list [--project …]  List droplets in a project
  doctl droplets create NAME …       Create a droplet with bootstrap cloud-config

Run from an application repo root to use digitalocean.* defaults in tooling.yaml."""
    )


def _main_impl() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        _print_help()
        sys.exit(0 if argv and argv[0] in ("-h", "--help") else 1)

    top = argv[0]
    rest = argv[1:]

    if top == "auth":
        if not rest or rest[0] in ("-h", "--help"):
            print("usage: doctl auth {init,list,remove,switch}", file=sys.stderr)
            sys.exit(0 if rest and rest[0] in ("-h", "--help") else 1)
        sub = rest[0]
        sub_argv = rest[1:]
        if sub == "init":
            sys.exit(_cmd_auth_init(sub_argv))
        if sub == "list":
            sys.exit(_cmd_auth_list(sub_argv))
        sys.exit(_cmd_auth_forward(sub, sub_argv))

    if top == "projects":
        if not rest or rest[0] in ("-h", "--help"):
            print("usage: doctl projects list", file=sys.stderr)
            sys.exit(0 if rest and rest[0] in ("-h", "--help") else 1)
        if rest[0] == "list":
            sys.exit(_cmd_projects_list(rest[1:]))
        print(f"unknown projects command: {rest[0]!r}", file=sys.stderr)
        sys.exit(1)

    if top == "cloud-config":
        if not rest or rest[0] in ("-h", "--help"):
            print("usage: doctl cloud-config print", file=sys.stderr)
            sys.exit(0 if rest and rest[0] in ("-h", "--help") else 1)
        if rest[0] == "print":
            sys.exit(_cmd_cloud_config_print(rest[1:]))
        print(f"unknown cloud-config command: {rest[0]!r}", file=sys.stderr)
        sys.exit(1)

    if top == "droplets":
        if not rest or rest[0] in ("-h", "--help"):
            print("usage: doctl droplets {list,create}", file=sys.stderr)
            sys.exit(0 if rest and rest[0] in ("-h", "--help") else 1)
        if rest[0] == "list":
            sys.exit(_cmd_droplets_list(rest[1:]))
        if rest[0] == "create":
            sys.exit(_cmd_droplets_create(rest[1:]))
        print(f"unknown droplets command: {rest[0]!r}", file=sys.stderr)
        sys.exit(1)

    print(f"unknown command: {top!r}", file=sys.stderr)
    _print_help()
    sys.exit(1)


def main() -> None:
    run_cli(_main_impl, label="doctl")
