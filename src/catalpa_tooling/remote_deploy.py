"""Managed Docker environments: ``docker/envs/<env>/info.yaml``, used by ``dk <env> …``."""

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

from catalpa_tooling.compose import _compose
from catalpa_tooling.dk_stack import compose_yml_build
from catalpa_tooling.env_yaml import _yaml_mapping_to_env
from catalpa_tooling.managed_deploy_env import (
    load_managed_deploy_context,
    print_managed_deploy_header,
    resolve_compose_file_from_info,
)
from catalpa_tooling.post_db_restore import run_post_db_restore_manage_commands
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.pgbackrest_db import (
    db_service_responds,
    ensure_db_service_running,
    pg_restore_extras_with_default_archive,
    run_backup as run_pgbackrest_backup_online,
    run_check_online,
    run_configure_verify_online_check,
    run_drop_create_app_database,
    run_info,
    run_pg_dump,
    run_pg_restore,
    run_restore_offline,
    run_version,
    _pg_restore_owner_acl_extras,
)
from catalpa_tooling.pgbackrest_volume_config import (
    ensure_external_stack_volumes,
    materialize_configs,
    postgres_image_from_env,
    remove_wipe_data_volumes,
    run_pgbackrest_stanza_create,
    run_pgbackrest_verify,
    should_materialize_for_compose,
)
from catalpa_tooling.media_pull import run_pull_media
from catalpa_tooling.trust_caddy_cert import trust_caddy_local_ca
from catalpa_tooling.media_rsync import (
    resolve_push_media_source,
    run_push_media_rsync,
)
from catalpa_tooling.restic_files import (
    merge_restic_verbose_from_cli,
    resolve_env_with_compose_project,
    run_backup,
    run_check,
    run_init,
    run_restore,
    run_snapshots,
    run_stats,
    split_restic_cli_verbose,
)
from catalpa_tooling.cli_confirm import confirm_by_typing_env_name
from catalpa_tooling.systemd_remote_install import (
    cmd_install_systemd_backups,
    parse_docker_host_to_ssh_target,
)
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.deploy_do_link import cmd_env_host, cmd_env_host_create
from catalpa_tooling.zabbix_systemd import run_zabbix_deploy
from catalpa_tooling.doctl_spaces_provision import (
    ensure_spaces_backup_credentials,
    needs_pgbr_write,
    needs_restic_write,
    pgbr_write_configured,
    restic_write_configured,
)


def _top_level_zbx_env_from_info(info: dict | None) -> dict[str, str]:
    """``zbx_*`` may be set at info.yaml top level so they are not merged into Compose ``env:``."""
    if not isinstance(info, dict):
        return {}
    subset = {
        k: v
        for k, v in info.items()
        if isinstance(k, str) and k.startswith("zbx_")
    }
    return _yaml_mapping_to_env(subset, skip_sops=False)


def _zabbix_env_defaults(
    info: dict | None,
    env_add: dict[str, str],
) -> dict[str, str]:
    """Keys from ``info.yaml`` ``env:`` and top-level ``zbx_*``, plus ``ZBX_*`` from deploy env (credentials).

    Duplicate keys: ``env:`` overrides top-level ``zbx_*``. Credentials ``env_add`` overrides both.
    """
    env_map = info.get("env") if isinstance(info, dict) else None
    env_map = env_map if isinstance(env_map, dict) else {}
    from_env = _yaml_mapping_to_env(env_map, skip_sops=False)
    from_top = _top_level_zbx_env_from_info(info)
    out = {**from_top, **from_env}
    plaintext_psk = {**from_top, **from_env}
    for psk_key in ("ZBX_TLSPSK", "ZBX_TLSPSKIDENTITY"):
        if (plaintext_psk.get(psk_key) or "").strip():
            print(
                "zabbix: TLS PSK secrets must be set in credentials.yaml "
                f"(`zbx_tlspsk` / `zbx_tlspskidentity`), not in info.yaml ({psk_key} is set in info).",
                file=sys.stderr,
            )
            break
    for k, v in env_add.items():
        if isinstance(k, str) and k.startswith("ZBX_"):
            out[k] = str(v)
    return out


def list_deploy_env_names(deploy_envs_dir: Path) -> list[str]:
    """Directory names under ``deploy_envs_dir`` that contain ``info.yaml`` (excludes ``_*``)."""
    root = deploy_envs_dir
    if not root.is_dir():
        return []
    names: list[str] = []
    for p in sorted(root.iterdir()):
        if p.name.startswith("_") or not p.is_dir():
            continue
        if (p / "info.yaml").is_file():
            names.append(p.name)
    return names


def _normalize_dk_env_argv(argv: list[str]) -> list[str]:
    """Rewrite argv for ``dk <env> …`` (no ``dk`` token).

    - ``<env> --help`` / ``-h`` (two args only) → ``--help`` (CLI help, not Compose).
    - ``<env> --dry-run`` → ``--dry-run <env>``
    - Trailing ``--yes`` / ``-y`` → moved to the front so it is not consumed by ``compose_args``.
    - ``<env> --tag <value>`` / ``<env> --tag=<value>`` (or the same after leading ``--dry-run``)
      → ``--tag <value> <env> …`` so ``--tag`` is not swallowed by ``compose_args`` (REMAINDER).
    """
    out = list(argv)
    if len(out) >= 2 and not out[0].startswith("-") and out[1] == "--dry-run":
        env = out[0]
        out = ["--dry-run", env] + out[2:]
    if len(out) >= 2 and out[-1] in ("--yes", "-y"):
        yes_flag = out.pop()
        out.insert(0, yes_flag)

    # Move `… <env> --tag <value> …` / `… <env> --tag=<value> …` before <env> (first match only).
    for i in range(len(out) - 1):
        if out[i].startswith("-") or i + 1 >= len(out):
            continue
        nxt = out[i + 1]
        tag_val: str | None = None
        rest_skip = 0
        if nxt == "--tag" and i + 2 < len(out):
            cand = out[i + 2]
            if cand.startswith("-"):
                continue
            tag_val = cand
            rest_skip = 3
        elif nxt.startswith("--tag="):
            tag_val = nxt.split("=", 1)[1]
            if not (tag_val or "").strip():
                continue
            rest_skip = 2
        else:
            continue
        env = out[i]
        out = out[:i] + ["--tag", tag_val.strip(), env] + out[i + rest_skip :]
        break

    if len(out) == 2 and not out[0].startswith("-") and out[1] in ("--help", "-h"):
        return ["--help"]
    return out


def _strip_dk_up_provision_flag(compose_args: list[str]) -> list[str]:
    """Remove dk-only ``--provision`` (volume ensure + materialize configs) before ``docker compose up``."""
    if not compose_args or compose_args[0] != "up":
        return compose_args
    return [a for a in compose_args if a != "--provision"]


def _is_compose_down_with_volumes(compose_args: list[str]) -> bool:
    """True if args run `docker compose down` with volume removal (-v / --volumes)."""
    if len(compose_args) < 1 or compose_args[0] != "down":
        return False
    return "-v" in compose_args or "--volumes" in compose_args


def _ensure_local_stack_images_built(
    config: ProjectConfig,
    env_add: dict[str, str],
    *,
    use_prepulled_registry: bool,
) -> int:
    """When not using pinned pre-pulled images, ``docker compose build`` stack images before volume init."""
    if use_prepulled_registry:
        return 0
    return compose_yml_build(config, env_add=env_add, services=None)


def _insert_up_build_if_no_registry(
    compose_args: list[str], *, use_prepulled_registry: bool
) -> list[str]:
    """When not using pre-pulled registry images, ensure ``up`` includes ``--build`` unless opted out."""
    if use_prepulled_registry:
        return compose_args
    if not compose_args or compose_args[0] != "up":
        return compose_args
    if "--build" in compose_args or "--no-build" in compose_args:
        return compose_args
    out = list(compose_args)
    i = 1
    flags_with_value = frozenset({"-t", "--timeout", "-p", "--profile", "--pull"})
    while i < len(out):
        arg = out[i]
        if not arg.startswith("-"):
            break
        i += 1
        if arg in flags_with_value and i < len(out) and not out[i].startswith("-"):
            i += 1
    out.insert(i, "--build")
    return out


def _global_dry_run_runs_systemd_install(peek: list[str]) -> bool:
    """True when global ``--dry-run`` should still load credentials (systemd install via SSH)."""
    if len(peek) >= 2 and peek[1] == "install-systemd":
        return peek[0] in ("bkp_db", "bkp_files")
    return False


def _dry_run_exits_before_compose_env(peek: list[str]) -> bool:
    """False when ``--dry-run`` should still resolve env and run (e.g. ``ensure_volumes``, systemd)."""
    if _global_dry_run_runs_systemd_install(peek):
        return False
    if len(peek) >= 1 and peek[0] in (
        "ensure_volumes",
        "trust-caddy-cert",
        "pull_media",
        "zabbix",
    ):
        return False
    if len(peek) >= 2 and peek[0] == "bkp_files" and peek[1] == "push":
        return False
    return True


def _confirm_deploy_wipe(env_name: str, site_origin: str, docker_host: str) -> bool:
    """Interactive guard: user must type the environment name."""
    print(
        "WARNING: This will run `docker compose down -v` on the deployment host, then remove "
        "external volumes for PostgreSQL data and Django media (database + uploads). "
        "Other external volumes (caddy_data, postgres_conf, pgbackrest_conf) are not removed.",
        file=sys.stderr,
    )
    print(f"  Environment: {env_name}", file=sys.stderr)
    print(f"  Site origin: {site_origin or '(none)'}", file=sys.stderr)
    print(f"  DOCKER_HOST: {docker_host or '(default local socket)'}", file=sys.stderr)
    return confirm_by_typing_env_name(env_name)


def _cmd_env_info(info_path: Path, tail: list[str], *, dry_run: bool) -> int:
    """Print or edit ``docker/envs/<env>/info.yaml``."""
    want_edit = "-e" in tail
    rest = [x for x in tail if x != "-e"]
    if rest:
        print("usage: dk <env> info [-e]", file=sys.stderr)
        return 1
    if want_edit:
        if dry_run:
            print(
                f"dry-run: would edit {info_path} ($EDITOR or vim).",
                file=sys.stderr,
            )
            return 0
        editor = (os.environ.get("EDITOR") or "").strip() or "vim"
        cmd = shlex.split(editor) + [str(info_path)]
        return subprocess.run(cmd).returncode
    with open(info_path, encoding="utf-8") as f:
        sys.stdout.write(f.read())
    return 0


def _cmd_env_host(
    env_name: str,
    config: ProjectConfig,
    tail: list[str],
    *,
    dry_run: bool,
) -> int:
    """Verify droplet / print or patch ``docker_host``; ``host create`` provisions a new droplet."""
    if tail and tail[0] == "create":
        return cmd_env_host_create(
            config,
            env_name,
            tail[1:],
            global_dry_run=dry_run,
        )

    p = argparse.ArgumentParser(prog=f"dk {env_name} host")
    p.add_argument(
        "--write",
        action="store_true",
        help="Write docker_host to info.yaml from the droplet public IPv4",
    )
    p.add_argument(
        "--sync-dns",
        action="store_true",
        help="Create or update DigitalOcean A records for site_origin hostnames",
    )
    ns, rest = p.parse_known_args(tail)
    if rest:
        p.error(f"unrecognized arguments: {' '.join(rest)}")
    if ns.write and ns.sync_dns:
        p.error("cannot use --write and --sync-dns together")
    return cmd_env_host(
        config,
        env_name,
        write=ns.write,
        sync_dns=ns.sync_dns,
        dry_run=dry_run,
    )


def _cmd_env_secrets(
    creds_path: Path,
    repo_root: Path,
    tail: list[str],
    *,
    dry_run: bool,
) -> int:
    """Edit ``docker/envs/<env>/credentials.yaml`` with SOPS (``sops``)."""
    if tail:
        print("usage: dk <env> secrets", file=sys.stderr)
        return 1
    try:
        creds_rel = str(creds_path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        creds_rel = str(creds_path)
    if dry_run:
        print(
            f"dry-run: would run sops {creds_rel} (cwd {repo_root})",
            file=sys.stderr,
        )
        return 0
    return subprocess.run(
        ["sops", creds_rel],
        cwd=repo_root,
        check=False,
    ).returncode


def _cmd_deploy(ns: argparse.Namespace, config: ProjectConfig) -> int:
    """Run docker compose (or backups) for docker/envs/<env>/info.yaml and credentials."""
    env_name = ns.env_name
    repo_root = config.repo_root
    deploy_dir = config.deploy_envs_dir / env_name
    info_path = deploy_dir / "info.yaml"
    creds_path = deploy_dir / "credentials.yaml"

    if not info_path.is_file():
        print(f"Missing {info_path}", file=sys.stderr)
        return 1

    with open(info_path, encoding="utf-8") as f:
        info = yaml.safe_load(f) or {}

    compose_args = list(getattr(ns, "compose_args", None) or [])
    if compose_args and compose_args[0] == "info":
        return _cmd_env_info(
            info_path,
            compose_args[1:],
            dry_run=bool(getattr(ns, "dry_run", False)),
        )

    if compose_args and compose_args[0] == "secrets":
        return _cmd_env_secrets(
            creds_path,
            repo_root,
            compose_args[1:],
            dry_run=bool(getattr(ns, "dry_run", False)),
        )

    if compose_args and compose_args[0] == "host":
        return _cmd_env_host(
            env_name,
            config,
            compose_args[1:],
            dry_run=bool(getattr(ns, "dry_run", False)),
        )

    compose_file = resolve_compose_file_from_info(info, config)
    if compose_file is None:
        return 1

    tag_override = getattr(ns, "tag", None)
    if getattr(ns, "dry_run", False) and _dry_run_exits_before_compose_env(compose_args):
        print_managed_deploy_header(
            config, env_name, info, compose_file, tag_override=tag_override
        )
        return 0

    ctx = load_managed_deploy_context(
        config,
        env_name,
        info=info,
        compose_file=compose_file,
        tag_override=tag_override,
    )
    if ctx is None:
        return 1
    env_add = ctx.env_add
    docker_host = ctx.docker_host
    site_origin = ctx.site_origin
    use_prepulled_registry = ctx.use_prepulled_registry

    env_add = resolve_env_with_compose_project(
        compose_file, env_add, config=config, dk_env_name=env_name
    )

    if compose_args and compose_args[0] == "zabbix":
        env_defaults = _zabbix_env_defaults(info, env_add)
        ssh_target: str | None
        try:
            ssh_target = parse_docker_host_to_ssh_target(str(docker_host))
        except ValueError:
            ssh_target = None
            print(
                "zabbix: docker_host is not SSH-formatted; running against this local machine.",
                file=sys.stderr,
            )
        return run_zabbix_deploy(
            compose_args[1:],
            config=config,
            prog=f"dk {env_name} zabbix",
            ssh_target=ssh_target,
            dry_run=bool(getattr(ns, "dry_run", False)),
            env_defaults=env_defaults,
            site_origin=str(site_origin) if site_origin else None,
        )

    if compose_args == ["wipe"]:
        compose_args = ["down", "-v"]

    if compose_args and compose_args[0] == "ensure_volumes":
        if len(compose_args) > 1:
            print("usage: dk <env> ensure_volumes", file=sys.stderr)
            return 1
        return ensure_external_stack_volumes(
            env_add,
            dry_run=bool(getattr(ns, "dry_run", False)),
            config=config,
        )

    if compose_args and compose_args[0] == "trust-caddy-cert":
        tail = compose_args[1:]
        if tail and tail != ["--dry-run"]:
            print(
                "usage: dk <env> trust-caddy-cert [--dry-run]",
                file=sys.stderr,
            )
            return 1
        dry = bool(getattr(ns, "dry_run", False)) or tail == ["--dry-run"]
        return trust_caddy_local_ca(
            compose_file,
            env_add,
            config,
            dry_run=dry,
        )

    if compose_args and compose_args[0] == "manage":
        manage_args = [a for a in compose_args[1:] if a]
        if not manage_args:
            print("usage: dk <env> manage <manage.py arguments>", file=sys.stderr)
            return 1
        return _compose(
            compose_file,
            "exec",
            config.stack_service("web"),
            "./manage.py",
            *manage_args,
            env_add=env_add,
        ).returncode

    if compose_args and compose_args[0] == "pull_media":
        pm = argparse.ArgumentParser(
            prog=f"dk {env_name} pull_media",
            description=(
                "Copy the django_media named volume to a local directory using docker run + tar. "
                "Respects DOCKER_HOST (e.g. SSH to the deploy host). Does not require restic."
            ),
        )
        pm.add_argument(
            "--target",
            "-t",
            default=None,
            help="Destination directory (default: paths.backend/media under repo root).",
        )
        pm.add_argument(
            "--image",
            default="alpine:3.21",
            help="Image containing tar (default: alpine:3.21).",
        )
        pm_args = pm.parse_args(compose_args[1:])
        raw_target = Path(pm_args.target or f"{config.paths.backend}/media").expanduser()
        target = raw_target.resolve() if raw_target.is_absolute() else (repo_root / raw_target).resolve()
        return run_pull_media(
            env_add,
            target=target,
            dry_run=bool(getattr(ns, "dry_run", False)),
            alpine_image=str(pm_args.image),
        )

    if compose_args and compose_args[0] == "bkp_files":
        bkp_files_extra = compose_args[1:]
        bkp_files_sub = bkp_files_extra[0] if bkp_files_extra else ""
        if bkp_files_sub == "push":
            pm = argparse.ArgumentParser(
                prog=f"dk {env_name} bkp_files push",
                description=(
                    "Rsync a host media directory into the django_media Compose volume "
                    "(default: dev.fetch_media.dest). Pair with `uv run dev fetch media`."
                ),
            )
            pm.add_argument(
                "--source",
                "-s",
                default=None,
                help="Host directory (default: paths.fetch_media.dest from tooling.yaml).",
            )
            pm.add_argument(
                "--method",
                choices=("rsync", "tar"),
                default="rsync",
                help="rsync (incremental) or tar via docker run (full stream).",
            )
            pm.add_argument(
                "--image",
                default="alpine:3.21",
                help="Alpine image for container rsync / tar fallback.",
            )
            pm_args, _unknown = pm.parse_known_args(bkp_files_extra[1:])
            source = resolve_push_media_source(config, repo_root, pm_args.source)
            if source is None:
                return 1
            rc = ensure_external_stack_volumes(
                env_add,
                dry_run=bool(getattr(ns, "dry_run", False)),
                config=config,
            )
            if rc != 0:
                return rc
            if not getattr(ns, "yes", False) and not getattr(ns, "dry_run", False):
                if not sys.stdin.isatty():
                    print(
                        "Refusing bkp_files push without a TTY. Pass --yes for non-interactive use.",
                        file=sys.stderr,
                    )
                    return 1
                print(
                    "WARNING: This mirrors host media into the deployment volume "
                    "(rsync --delete removes files on the volume that are not on the host).",
                    file=sys.stderr,
                )
                if not confirm_by_typing_env_name(env_name):
                    print("bkp_files push cancelled.", file=sys.stderr)
                    return 1
            return run_push_media_rsync(
                env_add,
                source=source,
                dry_run=bool(getattr(ns, "dry_run", False)),
                method=pm_args.method,
                alpine_image=str(pm_args.image),
            )
        if (
            bkp_files_sub
            and needs_restic_write(bkp_files_sub)
            and not restic_write_configured(env_add)
        ):
            rc = ensure_spaces_backup_credentials(
                config,
                env_name,
                env_add,
                creds_path,
                target="restic",
                command_label=f"dk {env_name} bkp_files",
                dry_run=bool(getattr(ns, "dry_run", False)),
                yes=bool(getattr(ns, "yes", False)),
            )
            if rc != 0:
                return rc
        raw_extra = compose_args[1:]
        if raw_extra and raw_extra[0] == "install-systemd":
            return cmd_install_systemd_backups(
                config,
                env_add,
                str(docker_host),
                env_name,
                raw_extra[1:],
                global_dry_run=bool(getattr(ns, "dry_run", False)),
                yes=bool(getattr(ns, "yes", False)),
                fixed_only="restic",
            )
        extra, cli_verbose = split_restic_cli_verbose(raw_extra)
        if not extra:
            print(
                "usage: bkp_files install-systemd [--dry-run] [--enable] | "
                "push [--source DIR] [--method rsync|tar] | "
                "init | backup | snapshots | check | stats | restore [SNAPSHOT]",
                file=sys.stderr,
            )
            print(
                "  Optional: repeat -v or --verbose for restic verbosity. "
                "Defaults: tooling.yaml ops.restic and docker/envs/<env>/info.yaml "
                "(pgbackrest: / restic: blocks or env:).",
                file=sys.stderr,
            )
            print(
                "  SNAPSHOT defaults to latest. Destructive restore: use global --yes to skip confirmation.",
                file=sys.stderr,
            )
            return 1
        sub = extra[0]
        env_r = merge_restic_verbose_from_cli(dict(env_add), cli_verbose)
        if sub == "init":
            if len(extra) > 1:
                print("Too many arguments for bkp_files init.", file=sys.stderr)
                return 1
            return run_init(env_r)
        if sub == "backup":
            if len(extra) > 1:
                print("Too many arguments for bkp_files backup.", file=sys.stderr)
                return 1
            return run_backup(env_r, config=config)
        if sub == "snapshots":
            if len(extra) > 1:
                print("Too many arguments for bkp_files snapshots.", file=sys.stderr)
                return 1
            return run_snapshots(env_r)
        if sub == "check":
            if len(extra) > 1:
                print("Too many arguments for bkp_files check.", file=sys.stderr)
                return 1
            return run_check(env_r)
        if sub == "stats":
            if len(extra) > 1:
                print("Too many arguments for bkp_files stats.", file=sys.stderr)
                return 1
            return run_stats(env_r)
        if sub == "restore":
            if len(extra) > 2:
                print("Too many arguments for bkp_files restore.", file=sys.stderr)
                return 1
            snap = (extra[1] if len(extra) > 1 else "latest").strip() or "latest"
            if not getattr(ns, "yes", False) and not sys.stdin.isatty():
                print(
                    "Refusing restore without a TTY. Pass --yes if you intend to run non-interactively.",
                    file=sys.stderr,
                )
                return 1
            return run_restore(
                env_r,
                snap,
                env_name=env_name,
                skip_confirm=bool(getattr(ns, "yes", False)),
                config=config,
            )
        print(f"Unknown bkp_files subcommand: {sub}", file=sys.stderr)
        print(
            "Use: bkp_files install-systemd [--dry-run] [--enable] | "
            "push [--source DIR] [--method rsync|tar] | "
            "init | backup | snapshots | check | stats | restore [SNAPSHOT]",
            file=sys.stderr,
        )
        return 1

    if compose_args and compose_args[0] == "bkp_db":
        extra = compose_args[1:]
        bkp_sub = extra[0] if extra else ""
        bkp_tail = extra[1:] if extra else []
        if bkp_sub and needs_pgbr_write(bkp_sub, bkp_tail) and not pgbr_write_configured(env_add):
            rc = ensure_spaces_backup_credentials(
                config,
                env_name,
                env_add,
                creds_path,
                target="pgbackrest",
                command_label=f"dk {env_name} bkp_db {bkp_sub}",
                dry_run=bool(getattr(ns, "dry_run", False)),
                yes=bool(getattr(ns, "yes", False)),
            )
            if rc != 0:
                return rc
        if not extra:
            print(
                "usage: bkp_db configure [verify|stanza-create] | "
                "install-systemd [--dry-run] [--enable] | "
                "info | check | version | backup full|incr|diff | pgdump [PG_DUMP_ARG ...] | "
                "pgrestore [--file ARCHIVE] [PG_RESTORE_ARG ...] | restore [pgBackRest restore args …]",
                file=sys.stderr,
            )
            print(
                "  Offline restore: use global --yes to skip confirmation in non-interactive use.",
                file=sys.stderr,
            )
            print(
                "  Log levels: tooling.yaml ops.pgbackrest and info.yaml pgbackrest: / env: "
                "(pgbr_log_level_console, …).",
                file=sys.stderr,
            )
            return 1
        sub = extra[0]
        if sub == "configure":
            rc = _ensure_local_stack_images_built(
                config,
                env_add,
                use_prepulled_registry=use_prepulled_registry,
            )
            if rc != 0:
                return rc
            img = postgres_image_from_env(env_add, config=config)
            print(f"Postgres image (volume ops / pgBackRest): {img}", file=sys.stderr)
            tail = extra[1:]
            rc = materialize_configs(env_add, dry_run=False, postgres_image=img, config=config)
            if rc != 0:
                return rc
            if not tail:
                return 0
            if tail == ["verify"]:
                rc = run_pgbackrest_verify(env_add, image=img)
                if rc != 0:
                    return rc
                return run_configure_verify_online_check(compose_file, env_add)
            if tail == ["stanza-create"]:
                return run_pgbackrest_stanza_create(env_add, image=img, config=config)
            print(
                f"Unknown bkp_db configure arguments: {' '.join(tail)}",
                file=sys.stderr,
            )
            print(
                "Use: bkp_db configure | bkp_db configure verify | bkp_db configure stanza-create",
                file=sys.stderr,
            )
            return 1
        if sub == "install-systemd":
            return cmd_install_systemd_backups(
                config,
                env_add,
                str(docker_host),
                env_name,
                extra[1:],
                global_dry_run=bool(getattr(ns, "dry_run", False)),
                yes=bool(getattr(ns, "yes", False)),
                fixed_only="pgbackrest",
            )
        if sub == "restore":
            restore_extra = extra[1:]
            if not getattr(ns, "yes", False) and not sys.stdin.isatty():
                print(
                    "Refusing restore without a TTY. Pass --yes if you intend to run non-interactively.",
                    file=sys.stderr,
                )
                return 1
            return run_restore_offline(
                env_add,
                compose_file=compose_file,
                env_name=env_name,
                skip_confirm=bool(getattr(ns, "yes", False)),
                extra_pgbackrest_args=restore_extra,
                config=config,
            )
        if sub == "backup":
            if len(extra) < 2:
                print("usage: bkp_db backup full|incr|diff", file=sys.stderr)
                return 1
            bt = extra[1]
            if bt not in ("full", "incr", "diff"):
                print(f"Invalid backup type: {bt!r}", file=sys.stderr)
                return 1
            if len(extra) > 2:
                print("Too many arguments for bkp_db backup.", file=sys.stderr)
                return 1
            if not db_service_responds(compose_file, env_add):
                print(
                    "The `db` service is not running on the deployment host. "
                    "Start the stack before online backup.",
                    file=sys.stderr,
                )
                return 1
            return run_pgbackrest_backup_online(compose_file, env_add, bt)
        if sub in ("info", "check", "version"):
            if len(extra) > 1:
                print(f"Too many arguments for bkp_db {sub}.", file=sys.stderr)
                return 1
            if not db_service_responds(compose_file, env_add):
                print(
                    "The `db` service is not running on the deployment host.",
                    file=sys.stderr,
                )
                return 1
            if sub == "info":
                return run_info(compose_file, env_add)
            if sub == "check":
                return run_check_online(compose_file, env_add)
            return run_version(compose_file, env_add)
        if sub == "pgdump":
            if not db_service_responds(compose_file, env_add):
                print(
                    "The `db` service is not running on the deployment host.",
                    file=sys.stderr,
                )
                return 1
            return run_pg_dump(compose_file, env_add, extra[1:])
        if sub == "pgrestore":
            restore_extras = _pg_restore_owner_acl_extras(
                pg_restore_extras_with_default_archive(
                    extra[1:],
                    config.fetch_db_dump_path,
                )
            )
            if "--file" not in restore_extras and sys.stdin.isatty():
                return 1
            rc = ensure_db_service_running(compose_file, env_add)
            if rc != 0:
                return rc
            print(
                "bkp_db pgrestore: replacing app database with an empty database before restore …",
                file=sys.stderr,
            )
            rc = run_drop_create_app_database(
                compose_file,
                env_add,
                postgis=config.dev.reset_db.postgis,
            )
            if rc != 0:
                return rc
            rc = run_pg_restore(compose_file, env_add, restore_extras)
            if rc != 0:
                return rc
            return run_post_db_restore_manage_commands(
                config,
                compose_file=compose_file,
                env_add=env_add,
                env_name=env_name,
            )
        print(f"Unknown bkp_db subcommand: {sub}", file=sys.stderr)
        print(
            "Use: bkp_db configure [verify|stanza-create] | "
            "install-systemd [--dry-run] [--enable] | "
            "info | check | version | backup full|incr|diff | pgdump [PG_DUMP_ARG ...] | "
            "pgrestore [--file ARCHIVE] [PG_RESTORE_ARG ...] | restore [pgBackRest restore args …]",
            file=sys.stderr,
        )
        return 1

    if not compose_args:
        compose_args = ["up", "-d"]

    if should_materialize_for_compose(compose_args):
        rc = ensure_external_stack_volumes(env_add, dry_run=False, config=config)
        if rc != 0:
            return rc
        rc = _ensure_local_stack_images_built(
            config,
            env_add,
            use_prepulled_registry=use_prepulled_registry,
        )
        if rc != 0:
            return rc
        rc = materialize_configs(
            env_add,
            dry_run=False,
            postgres_image=postgres_image_from_env(env_add, config=config),
            config=config,
        )
        if rc != 0:
            return rc

    if _is_compose_down_with_volumes(compose_args):
        if not getattr(ns, "yes", False):
            if not sys.stdin.isatty():
                print(
                    "Refusing to wipe a managed deploy without a TTY. "
                    "Pass --yes if you intend to run non-interactively.",
                    file=sys.stderr,
                )
                return 1
            if not _confirm_deploy_wipe(env_name, site_origin, docker_host):
                print("Wipe cancelled.", file=sys.stderr)
                return 1

    compose_args = _strip_dk_up_provision_flag(compose_args)

    if use_prepulled_registry and compose_args and compose_args[0] == "up":
        compose_args = ["up", "-d", "--pull", "missing", "--no-build"]
    else:
        compose_args = _insert_up_build_if_no_registry(
            compose_args,
            use_prepulled_registry=use_prepulled_registry,
        )
    proc = _compose(
        compose_file,
        *compose_args,
        env_add=env_add,
        check=False,
    )
    if proc.returncode != 0:
        return proc.returncode
    if _is_compose_down_with_volumes(compose_args):
        return remove_wipe_data_volumes(env_add, config=config)
    return 0
