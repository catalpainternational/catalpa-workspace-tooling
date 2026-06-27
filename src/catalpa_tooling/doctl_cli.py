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
from catalpa_tooling.cloud_config.render import render_droplet_bootstrap
from catalpa_tooling.deploy_do_link import (
    cmd_env_host,
    cmd_env_host_create,
    droplet_name_for_env,
    normalize_droplet_hostname,
)
from catalpa_tooling.doctl_droplets import create_droplet
from catalpa_tooling.doctl_projects import (
    list_project_droplets,
    resolve_project_id,
    resolve_project_id_dry_run,
)

PROG = "dk digoc"


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


def _cmd_auth_init(ns_or_argv: argparse.Namespace | list[str]) -> int:
    if isinstance(ns_or_argv, list):
        return run_digoc(["auth", "init", *ns_or_argv])
    ns = ns_or_argv
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
    ensure_doctl_available()
    return run_doctl(["auth", subcommand, *argv]).returncode


def _cmd_auth_list(ns_or_argv: argparse.Namespace | list[str]) -> int:
    if isinstance(ns_or_argv, list):
        return run_digoc(["auth", "list", *ns_or_argv])
    ns = ns_or_argv
    ensure_doctl_available()
    return run_doctl(["auth", "list"], context=ns.context).returncode


def _cmd_projects_list(ns_or_argv: argparse.Namespace | list[str]) -> int:
    if isinstance(ns_or_argv, list):
        return run_digoc(["projects", "list", *ns_or_argv])
    ns = ns_or_argv
    ensure_doctl_available()
    return run_doctl(
        ["projects", "list", "--format", ns.format],
        context=ns.context,
    ).returncode


def _cmd_cloud_config_print(ns_or_argv: argparse.Namespace | list[str]) -> int:
    if isinstance(ns_or_argv, list):
        return run_digoc(["cloud-config", "print", *ns_or_argv])
    ns = ns_or_argv
    print(render_droplet_bootstrap(timezone=ns.timezone), end="")
    return 0


def _cmd_droplets_create(ns_or_argv: argparse.Namespace | list[str]) -> int:
    if isinstance(ns_or_argv, list):
        return run_digoc(["droplets", "create", *ns_or_argv])
    ns = ns_or_argv
    droplet_name = normalize_droplet_hostname((ns.name or "").strip()) if ns.name else ""
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
        print("NAME is required (env droplets: dk <env> host create)", file=sys.stderr)
        return 2

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


def _cmd_droplets_list(ns_or_argv: argparse.Namespace | list[str]) -> int:
    if isinstance(ns_or_argv, list):
        return run_digoc(["droplets", "list", *ns_or_argv])
    ns = ns_or_argv
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


def _cmd_droplets_suggest_env(ns_or_argv: argparse.Namespace | list[str]) -> int:
    if isinstance(ns_or_argv, list):
        return run_digoc(["droplets", "suggest-env", *ns_or_argv])
    ns = ns_or_argv
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


def dispatch_digoc(ns: argparse.Namespace) -> int:
    """Dispatch parsed ``dk digoc …`` namespace to handler implementations."""
    try:
        handler = getattr(ns, "handler", None)
        if handler == "auth-init":
            return _cmd_auth_init(ns)
        if handler == "auth-list":
            return _cmd_auth_list(ns)
        if handler == "auth-forward":
            sub = getattr(ns, "auth_forward_sub", None) or ns.auth_command
            return _cmd_auth_forward(sub, [])
        if handler == "projects-list":
            return _cmd_projects_list(ns)
        if handler == "cloud-config-print":
            return _cmd_cloud_config_print(ns)
        if handler == "droplets-list":
            return _cmd_droplets_list(ns)
        if handler == "droplets-create":
            return _cmd_droplets_create(ns)
        if handler == "droplets-suggest-env":
            return _cmd_droplets_suggest_env(ns)
        print(f"dk digoc: unknown handler {handler!r}", file=sys.stderr)
        return 1
    except DoctlNotFoundError as e:
        print_doctl_required(e)
        return 1
    except DoctlCommandError as e:
        print(str(e), file=sys.stderr)
        return e.returncode


def run_digoc(argv: list[str]) -> int:
    """Legacy argv dispatch for tests; prefer ``dispatch_digoc`` after parsing."""
    from catalpa_tooling.digoc_parser import build_digoc_parser

    config = None
    try:
        config = ProjectConfig.from_cwd()
    except (ProjectConfigError, FileNotFoundError):
        pass

    if not argv:
        build_digoc_parser(config).print_help()
        return 1
    if argv[0] in ("-h", "--help"):
        build_digoc_parser(config).print_help()
        return 0

    parser = build_digoc_parser(config)
    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 1
        return int(code) if int(code) != 0 else 1
    return dispatch_digoc(ns)
