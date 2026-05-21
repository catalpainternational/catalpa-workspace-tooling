"""DigitalOcean helpers for ``dk digoc`` (wraps the official host ``doctl`` binary)."""

from __future__ import annotations

import argparse
import sys

from catalpa_tooling.config import DigitalOceanConfig, ProjectConfig, ProjectConfigError
from catalpa_tooling.doctl_binary import (
    DoctlCommandError,
    DoctlNotFoundError,
    ensure_doctl_available,
    print_doctl_required,
    run_doctl,
)
from catalpa_tooling.cloud_config.render import DEFAULT_TIMEZONE, render_droplet_bootstrap
from catalpa_tooling.deploy_do_link import (
    cmd_env_host,
    cmd_env_host_create,
    droplet_name_for_env,
)
from catalpa_tooling.doctl_droplets import create_droplet
from catalpa_tooling.doctl_projects import (
    list_project_droplets,
    resolve_project_id,
    resolve_project_id_dry_run,
)

PROG = "dk digoc"


def _cmd_auth_init(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog=f"{PROG} auth init")
    p.add_argument(
        "--context",
        help="Named authentication context (default: default)",
    )
    p.add_argument(
        "-t",
        "--access-token",
        dest="access_token",
        metavar="TOKEN",
        help="API token (non-interactive; forwarded to host doctl)",
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
        args.append("--interactive")
    else:
        print(
            f"{PROG} auth init: not a TTY and no --access-token given.\n"
            "Run in a terminal, pass -t TOKEN, or clear a bad token with:\n"
            "  dk digoc auth remove --context default",
            file=sys.stderr,
        )
        return 1
    return run_doctl(args).returncode


def _cmd_auth_forward(subcommand: str, argv: list[str]) -> int:
    """Pass through to host ``doctl auth <subcommand>`` (remove, switch, …)."""
    ensure_doctl_available()
    return run_doctl(["auth", subcommand, *argv]).returncode


def _cmd_auth_list(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog=f"{PROG} auth list")
    p.add_argument("--context", help="Unused; listed for symmetry with other subcommands")
    ns, rest = p.parse_known_args(argv)
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")
    ensure_doctl_available()
    return run_doctl(["auth", "list"], context=ns.context).returncode


def _cmd_projects_list(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog=f"{PROG} projects list")
    p.add_argument("--context", help="Authentication context")
    p.add_argument(
        "--format",
        default="ID,Name,Purpose",
        help="Columns for host doctl output (default: ID,Name,Purpose)",
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
        prog=f"{PROG} cloud-config print",
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


def _forward_host_create_argv(ns: argparse.Namespace) -> list[str]:
    """Build argv for ``cmd_env_host_create`` from parsed digoc create flags."""
    out: list[str] = []
    if ns.project:
        out.extend(["--project", ns.project])
    if ns.size:
        out.extend(["--size", ns.size])
    if ns.image:
        out.extend(["--image", ns.image])
    if ns.region:
        out.extend(["--region", ns.region])
    for key in ns.ssh_keys or []:
        out.extend(["--ssh-key", key])
    if ns.timezone:
        out.extend(["--timezone", ns.timezone])
    if ns.context:
        out.extend(["--context", ns.context])
    if ns.dry_run:
        out.append("--dry-run")
    if ns.no_monitoring:
        out.append("--no-monitoring")
    return out


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
        prog=f"{PROG} droplets create",
        description="Create a droplet with Docker CE, UFW, unattended upgrades, and SSH hardening.",
    )
    p.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Droplet hostname (default with --for-env: digitalocean.droplet_name or {project}-{env})",
    )
    p.add_argument(
        "--for-env",
        metavar="ENV",
        help="Use resolved droplet name for ENV (info.yaml override or {project}-{env}) when NAME is omitted",
    )
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
        help="SSH key ID or fingerprint (repeatable; default: all keys from host doctl)",
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
        help="Print cloud-config and host doctl command without creating",
    )
    p.add_argument(
        "--no-monitoring",
        action="store_true",
        help="Omit --enable-monitoring (default: install DO metrics agent)",
    )
    ns, rest = p.parse_known_args(argv)
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")

    droplet_name = (ns.name or "").strip() if ns.name else ""
    for_env = (ns.for_env or "").strip()

    cfg: ProjectConfig | None = None
    try:
        cfg = ProjectConfig.from_cwd()
    except (ProjectConfigError, FileNotFoundError) as e:
        if for_env:
            print(str(e), file=sys.stderr)
            return 1
    if for_env:
        if cfg is None:
            print("tooling.yaml required when using --for-env", file=sys.stderr)
            return 1
        env_dir = cfg.deploy_envs_dir / for_env
        if not env_dir.is_dir() or not (env_dir / "info.yaml").is_file():
            print(f"Missing deploy environment: {env_dir / 'info.yaml'}", file=sys.stderr)
            return 1
        from_name = droplet_name_for_env(cfg, for_env)
        if not from_name:
            print(f"Missing deploy environment info: {env_dir / 'info.yaml'}", file=sys.stderr)
            return 1
        if droplet_name and droplet_name != from_name:
            print(
                f"NAME {droplet_name!r} does not match --for-env {for_env!r} "
                f"droplet_name {from_name!r}; omit NAME or use `dk {for_env} host create`.",
                file=sys.stderr,
            )
            return 1
        if ns.wait:
            print(
                f"Note: --wait is always enabled for env droplet create; "
                f"use `dk {for_env} host create` instead.",
                file=sys.stderr,
            )
        return cmd_env_host_create(
            cfg,
            for_env,
            _forward_host_create_argv(ns),
            global_dry_run=False,
            deprecation_message=(
                f"Deprecated: use `dk {for_env} host create` instead of "
                f"`dk digoc droplets create --for-env {for_env}`."
            ),
        )

    if not droplet_name:
        p.error("NAME is required (env droplets: dk <env> host create)")

    do_config, manifest_context = _load_do_config_for_droplets(project_flag=ns.project)
    if cfg is not None and do_config is None:
        do_config = cfg.digitalocean
    context = ns.context or manifest_context
    if ns.dry_run:
        project_id = resolve_project_id_dry_run(ns.project, do_config=do_config)
    else:
        ensure_doctl_available()
        project_id = resolve_project_id(ns.project, do_config=do_config, context=context)
    return create_droplet(
        droplet_name,
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
        for_env=None,
        enable_monitoring=False if ns.no_monitoring else None,
    )


def _cmd_droplets_list(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog=f"{PROG} droplets list",
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
    cfg: ProjectConfig | None = None
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
        config=cfg,
    )


def _cmd_droplets_suggest_env(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog=f"{PROG} droplets suggest-env",
        description="Print suggested docker_host for a dk deploy environment (same as dk <env> host).",
    )
    p.add_argument("env", help="Deploy environment name (docker/envs/<env>/)")
    p.add_argument(
        "--write",
        action="store_true",
        help="Write docker_host to info.yaml",
    )
    ns, rest = p.parse_known_args(argv)
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")
    try:
        cfg = ProjectConfig.from_cwd()
    except ProjectConfigError as e:
        print(str(e), file=sys.stderr)
        return 1
    env = ns.env.strip()
    print(
        f"Deprecated: use `dk {env} host` instead of `dk digoc droplets suggest-env {env}`.",
        file=sys.stderr,
    )
    return cmd_env_host(cfg, env, write=ns.write, dry_run=False)


def _print_help() -> None:
    print(
        f"""usage: {PROG} [-h] {{auth,cloud-config,projects,droplets}} ...

DigitalOcean helpers for dk deploy (requires the official doctl binary on PATH or DOCTL_BIN).

  {PROG} auth init [--context NAME] [-t TOKEN]   Initialize API token (interactive)
  {PROG} auth list                    List authentication contexts
  {PROG} auth remove --context NAME   Remove a stored context
  {PROG} auth switch --context NAME   Switch active context
  {PROG} cloud-config print           Print droplet bootstrap cloud-config
  {PROG} projects list                List DigitalOcean projects
  {PROG} droplets list [--project …]  List droplets in a project (Env column when tooling.yaml present)
  {PROG} droplets create [NAME] …     Create a droplet (env: use dk <env> host create)
  {PROG} droplets suggest-env ENV     Deprecated; use dk <env> host

Run from an application repo root to use digitalocean.* defaults in tooling.yaml."""
    )


def _run_digoc_impl(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        _print_help()
        return 0 if argv and argv[0] in ("-h", "--help") else 1

    top = argv[0]
    rest = argv[1:]

    if top == "auth":
        if not rest or rest[0] in ("-h", "--help"):
            print(f"usage: {PROG} auth {{init,list,remove,switch}}", file=sys.stderr)
            return 0 if rest and rest[0] in ("-h", "--help") else 1
        sub = rest[0]
        sub_argv = rest[1:]
        if sub == "init":
            return _cmd_auth_init(sub_argv)
        if sub == "list":
            return _cmd_auth_list(sub_argv)
        return _cmd_auth_forward(sub, sub_argv)

    if top == "projects":
        if not rest or rest[0] in ("-h", "--help"):
            print(f"usage: {PROG} projects list", file=sys.stderr)
            return 0 if rest and rest[0] in ("-h", "--help") else 1
        if rest[0] == "list":
            return _cmd_projects_list(rest[1:])
        print(f"unknown projects command: {rest[0]!r}", file=sys.stderr)
        return 1

    if top == "cloud-config":
        if not rest or rest[0] in ("-h", "--help"):
            print(f"usage: {PROG} cloud-config print", file=sys.stderr)
            return 0 if rest and rest[0] in ("-h", "--help") else 1
        if rest[0] == "print":
            return _cmd_cloud_config_print(rest[1:])
        print(f"unknown cloud-config command: {rest[0]!r}", file=sys.stderr)
        return 1

    if top == "droplets":
        if not rest or rest[0] in ("-h", "--help"):
            print(f"usage: {PROG} droplets {{list,create,suggest-env}}", file=sys.stderr)
            return 0 if rest and rest[0] in ("-h", "--help") else 1
        if rest[0] == "list":
            return _cmd_droplets_list(rest[1:])
        if rest[0] == "create":
            return _cmd_droplets_create(rest[1:])
        if rest[0] == "suggest-env":
            return _cmd_droplets_suggest_env(rest[1:])
        print(f"unknown droplets command: {rest[0]!r}", file=sys.stderr)
        return 1

    print(f"unknown command: {top!r}", file=sys.stderr)
    _print_help()
    return 1


def run_digoc(argv: list[str]) -> int:
    """Dispatch ``dk digoc …`` subcommands. Returns an exit code (does not call ``sys.exit``)."""
    try:
        return _run_digoc_impl(argv)
    except DoctlNotFoundError as e:
        print_doctl_required(e)
        return 1
    except DoctlCommandError as e:
        print(str(e), file=sys.stderr)
        return e.returncode
