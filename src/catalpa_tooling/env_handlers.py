"""Dispatch handlers for ``dk <env> …`` (parsed by ``env_parser``)."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

from catalpa_tooling.compose import _compose
from catalpa_tooling.deprecation import warn_deprecated
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.deploy_do_link import cmd_env_host, cmd_env_host_create
from catalpa_tooling.db_restore import run_unified_db_restore
from catalpa_tooling.doctl_spaces_provision import (
    ensure_spaces_backup_credentials,
    needs_pgbr_write,
    needs_restic_write,
    pgbr_write_configured,
    restic_write_configured,
)
from catalpa_tooling.managed_deploy_env import (
    load_managed_deploy_context,
    print_managed_deploy_header,
    resolve_compose_file_from_info,
)
from catalpa_tooling.media_pull import run_pull_media
from catalpa_tooling.media_rsync import resolve_push_media_source, run_push_media_rsync
from catalpa_tooling.pgbackrest_db import (
    compose_pg_restore_extras_for_config,
    db_service_responds,
    ensure_db_service_running,
    run_backup as run_pgbackrest_backup_online,
    run_bkp_db_init,
    run_bkp_db_stanza_create_flow,
    run_check_online,
    run_configure_verify_online_check,
    run_drop_create_app_database,
    run_info,
    run_pg_dump,
    run_pg_restore,
    run_version,
)
from catalpa_tooling.pgbackrest_volume_config import (
    ensure_external_stack_volumes,
    materialize_configs,
    postgres_image_from_env,
    remove_wipe_data_volumes,
    run_pgbackrest_verify,
    should_materialize_for_compose,
)
from catalpa_tooling.post_db_restore import run_post_db_restore_manage_commands
from catalpa_tooling.remote_deploy import (
    _confirm_deploy_wipe,
    _dry_run_exits_before_compose_env,
    _ensure_local_stack_images_built,
    _insert_down_remove_orphans,
    _insert_up_build_if_no_registry,
    _insert_up_prepulled_pull_flags,
    _is_compose_down_with_volumes,
    _strip_dk_up_provision_flag,
    _top_level_zbx_env_from_info,
    _zabbix_env_defaults,
    resolve_deploy_env_name,
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
from catalpa_tooling.host_storage import ensure_host_storage
from catalpa_tooling.dc_backup.hosts import (
    dc_backup_tls_extra_compose_files,
    merge_extra_compose_files,
)
from catalpa_tooling.local_proxy import (
    LocalProxyConfigError,
    local_proxy_extra_compose_files,
    sync_local_proxy_for_compose_action,
)
from catalpa_tooling.trust_caddy_cert import trust_caddy_local_ca
from catalpa_tooling.zabbix_systemd import run_zabbix_deploy


def _cmd_env_info(info_path: Path, *, edit: bool, dry_run: bool) -> int:
    if edit:
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


def _cmd_env_secrets(creds_path: Path, repo_root: Path, *, dry_run: bool) -> int:
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


def _zabbix_argv_from_ns(ns: argparse.Namespace) -> list[str]:
    cmd = ns.zabbix_command
    if cmd == "install":
        argv = ["install"]
        if ns.image:
            argv.extend(["--image", ns.image])
        if ns.server:
            argv.extend(["--server", ns.server])
        if ns.hostname:
            argv.extend(["--hostname", ns.hostname])
        if ns.active_allow is not None:
            argv.append("--active-allow" if ns.active_allow else "--no-active-allow")
        if ns.docker_group_gid is not None:
            argv.extend(["--docker-group-gid", str(ns.docker_group_gid)])
        if getattr(ns, "dry_run", False):
            argv.append("--dry-run")
        if getattr(ns, "force", False):
            argv.append("--force")
        return argv
    if cmd in ("enable", "disable", "restart"):
        argv = [cmd]
        if getattr(ns, "dry_run", False):
            argv.append("--dry-run")
        return argv
    if cmd == "logs":
        argv = ["logs", "-n", str(ns.lines)]
        if ns.follow:
            argv.append("-f")
        return argv
    return [cmd]


def _ensure_stack_volumes(
    config: ProjectConfig,
    env_name: str,
    info: dict,
    env_add: dict[str, str],
    storage_volumes: dict,
    *,
    dry_run: bool = False,
) -> int:
    if storage_volumes:
        return ensure_host_storage(
            config,
            env_name,
            info,
            storage_volumes,
            env_add=env_add,
            dry_run=dry_run,
        )
    return ensure_external_stack_volumes(env_add, dry_run=dry_run, config=config)


def _run_compose_path(
    ns: argparse.Namespace,
    config: ProjectConfig,
    *,
    info: dict,
    compose_file: str,
    env_add: dict[str, str],
    env_name: str,
    compose_args: list[str],
    use_prepulled_registry: bool,
    site_origin: str,
    docker_host: str,
    storage_volumes: dict,
) -> int:
    repo_root = config.repo_root
    dry_run = bool(getattr(ns, "dry_run", False))

    if compose_args == ["wipe"]:
        compose_args = ["down", "-v"]

    if should_materialize_for_compose(compose_args):
        rc = _ensure_stack_volumes(
            config,
            env_name,
            info,
            env_add,
            storage_volumes,
            dry_run=False,
        )
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
        if not ns.yes:
            if not sys.stdin.isatty():
                print(
                    "Refusing to wipe a managed deploy without a TTY. "
                    "Pass --yes if you intend to run non-interactive.",
                    file=sys.stderr,
                )
                return 1
            if not _confirm_deploy_wipe(env_name, site_origin, docker_host):
                print("Wipe cancelled.", file=sys.stderr)
                return 1

    compose_args = _strip_dk_up_provision_flag(compose_args)
    compose_args = _insert_down_remove_orphans(compose_args)

    try:
        if compose_args and compose_args[0] == "up":
            rc = sync_local_proxy_for_compose_action(
                info,
                config,
                env_name,
                compose_args,
                env_add,
                dry_run=dry_run,
            )
            if rc != 0:
                return rc
    except LocalProxyConfigError as e:
        print(str(e), file=sys.stderr)
        return 1

    if use_prepulled_registry:
        compose_args = _insert_up_prepulled_pull_flags(
            compose_args,
            use_prepulled_registry=use_prepulled_registry,
        )
    else:
        compose_args = _insert_up_build_if_no_registry(
            compose_args,
            use_prepulled_registry=use_prepulled_registry,
        )
    try:
        proxy_files = local_proxy_extra_compose_files(
            info,
            config,
            env_name,
            env_add,
            compose_args,
        )
    except LocalProxyConfigError as e:
        print(str(e), file=sys.stderr)
        return 1
    try:
        tls_files = dc_backup_tls_extra_compose_files(
            info,
            config,
            env_name,
            env_add,
            compose_args,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    extra_compose_files = merge_extra_compose_files(proxy_files, tls_files)
    proc = _compose(
        compose_file,
        *compose_args,
        env_add=env_add,
        extra_compose_files=extra_compose_files,
        check=False,
    )
    if proc.returncode != 0:
        return proc.returncode
    if compose_args and compose_args[0] == "down":
        try:
            rc = sync_local_proxy_for_compose_action(
                info,
                config,
                env_name,
                compose_args,
                env_add,
                dry_run=dry_run,
            )
            if rc != 0:
                return rc
        except LocalProxyConfigError as e:
            print(str(e), file=sys.stderr)
            return 1
    if _is_compose_down_with_volumes(compose_args):
        return remove_wipe_data_volumes(env_add, config=config)
    return 0


def handle_env_command(ns: argparse.Namespace, config: ProjectConfig) -> int:
    """Run docker compose (or backups) for docker/envs/<env>/info.yaml and credentials."""
    env_name = resolve_deploy_env_name(config, ns.env_name)
    repo_root = config.repo_root
    deploy_dir = config.deploy_envs_dir / env_name
    info_path = deploy_dir / "info.yaml"
    creds_path = deploy_dir / "credentials.yaml"

    if not info_path.is_file():
        print(f"Missing {info_path}", file=sys.stderr)
        return 1

    with open(info_path, encoding="utf-8") as f:
        info = yaml.safe_load(f) or {}

    env_command = getattr(ns, "env_command", None)
    dry_run = bool(getattr(ns, "dry_run", False))
    tag_override = getattr(ns, "tag", None)

    if env_command is None:
        compose_args = list(getattr(ns, "implicit_compose_argv", None) or [])
        if not compose_args:
            compose_args = ["up", "-d"]
    elif env_command == "compose":
        compose_args = list(getattr(ns, "compose_argv", None) or [])
        if not compose_args:
            compose_args = ["up", "-d"]
    else:
        compose_args = []

    if env_command == "info":
        return _cmd_env_info(info_path, edit=bool(getattr(ns, "edit", False)), dry_run=dry_run)

    if env_command == "secrets":
        return _cmd_env_secrets(creds_path, repo_root, dry_run=dry_run)

    if env_command == "dc-backup":
        from catalpa_tooling.dc_backup.cli import handle_dc_backup_command

        return handle_dc_backup_command(ns, config, env_name, dry_run=dry_run)

    if env_command == "host":
        if getattr(ns, "host_command", None) == "create":
            tail = list(getattr(ns, "host_create_args", None) or [])
            return cmd_env_host_create(
                config,
                env_name,
                tail,
                global_dry_run=dry_run,
            )
        if ns.write and ns.sync_dns:
            print("cannot use --write and --sync-dns together", file=sys.stderr)
            return 2
        return cmd_env_host(
            config,
            env_name,
            write=bool(ns.write),
            sync_dns=bool(ns.sync_dns),
            dry_run=dry_run,
            check_remote=bool(getattr(ns, "check_remote", False)),
        )

    compose_file = resolve_compose_file_from_info(info, config)
    if compose_file is None and env_command not in (None, "compose"):
        return 1

    peek = compose_args if env_command in (None, "compose") else [env_command or ""]
    if env_command == "wipe":
        peek = ["down", "-v"]

    if dry_run and _dry_run_exits_before_compose_env(peek) and env_command in (None, "compose"):
        if compose_file is None:
            return 1
        print_managed_deploy_header(
            config, env_name, info, compose_file, tag_override=tag_override
        )
        return 0

    if env_command in (None, "compose") and compose_file is None:
        return 1

    ctx = load_managed_deploy_context(
        config,
        env_name,
        info=info,
        compose_file=compose_file or "",
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

    if env_command == "zabbix":
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
            _zabbix_argv_from_ns(ns),
            config=config,
            prog=f"dk {env_name} zabbix",
            ssh_target=ssh_target,
            dry_run=dry_run,
            env_defaults=env_defaults,
            site_origin=str(site_origin) if site_origin else None,
        )

    if env_command == "ensure_volumes":
        return _ensure_stack_volumes(
            config,
            env_name,
            info,
            env_add,
            ctx.storage_volumes,
            dry_run=dry_run,
        )

    if env_command == "storage":
        if getattr(ns, "storage_command", None) != "ensure":
            print("usage: dk <env> storage ensure", file=sys.stderr)
            return 1
        return _ensure_stack_volumes(
            config,
            env_name,
            info,
            env_add,
            ctx.storage_volumes,
            dry_run=dry_run,
        )

    if env_command == "trust-caddy-cert":
        return trust_caddy_local_ca(
            compose_file,
            env_add,
            config,
            dry_run=dry_run,
            info=info,
        )

    if env_command == "manage":
        manage_args = [a for a in getattr(ns, "manage_args", []) if a]
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

    if env_command == "pull_media":
        raw_target = Path(ns.target or f"{config.paths.backend}/media").expanduser()
        target = raw_target.resolve() if raw_target.is_absolute() else (repo_root / raw_target).resolve()
        return run_pull_media(
            env_add,
            target=target,
            dry_run=dry_run,
            alpine_image=str(ns.image),
            config=config,
        )

    if env_command in ("files", "bkp_files"):
        if env_command == "bkp_files":
            warn_deprecated("bkp_files", "files", context=f"dk {env_name}")
        return _handle_bkp_files(
            ns, config, env_name, env_add, creds_path, docker_host, dry_run, info, ctx.storage_volumes
        )

    if env_command in ("db", "bkp_db"):
        if env_command == "bkp_db":
            warn_deprecated("bkp_db", "db", context=f"dk {env_name}")
        return _handle_bkp_db(
            ns,
            config,
            env_name,
            env_add,
            creds_path,
            compose_file,
            docker_host,
            dry_run,
            use_prepulled_registry=use_prepulled_registry,
        )

    if env_command == "wipe":
        compose_args = ["down", "-v"]

    return _run_compose_path(
        ns,
        config,
        info=info,
        compose_file=compose_file,
        env_add=env_add,
        env_name=env_name,
        compose_args=compose_args,
        use_prepulled_registry=use_prepulled_registry,
        site_origin=site_origin,
        docker_host=docker_host,
        storage_volumes=ctx.storage_volumes,
    )


def _handle_bkp_files(
    ns: argparse.Namespace,
    config: ProjectConfig,
    env_name: str,
    env_add: dict[str, str],
    creds_path: Path,
    docker_host: str,
    dry_run: bool,
    info: dict,
    storage_volumes: dict,
) -> int:
    repo_root = config.repo_root
    sub = getattr(ns, "files_command", None) or getattr(ns, "bkp_files_command", None)

    if sub == "push":
        source = resolve_push_media_source(config, repo_root, ns.source)
        if source is None:
            return 1
        rc = _ensure_stack_volumes(
            config,
            env_name,
            info,
            env_add,
            storage_volumes,
            dry_run=dry_run,
        )
        if rc != 0:
            return rc
        if not ns.yes and not dry_run:
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
            dry_run=dry_run,
            method=ns.method,
            alpine_image=str(ns.image),
            config=config,
        )

    if sub and needs_restic_write(sub) and not restic_write_configured(env_add):
        rc = ensure_spaces_backup_credentials(
            config,
            env_name,
            env_add,
            creds_path,
            target="restic",
            command_label=f"dk {env_name} files",
            dry_run=dry_run,
            yes=bool(ns.yes),
        )
        if rc != 0:
            return rc

    if sub == "install-systemd":
        tail: list[str] = []
        if getattr(ns, "dry_run", False):
            tail.append("--dry-run")
        if getattr(ns, "enable", False):
            tail.append("--enable")
        return cmd_install_systemd_backups(
            config,
            env_add,
            str(docker_host),
            env_name,
            tail,
            global_dry_run=dry_run,
            yes=bool(ns.yes),
            fixed_only="restic",
        )

    env_r = dict(env_add)
    if sub == "init":
        return run_init(env_r)
    if sub == "backup":
        return run_backup(env_r, config=config)
    if sub == "snapshots":
        return run_snapshots(env_r)
    if sub == "check":
        return run_check(env_r)
    if sub == "stats":
        return run_stats(env_r)
    if sub == "restore":
        snap = (getattr(ns, "snapshot", None) or "latest").strip() or "latest"
        if not ns.yes and not sys.stdin.isatty():
            print(
                "Refusing restore without a TTY. Pass --yes if you intend to run non-interactive.",
                file=sys.stderr,
            )
            return 1
        return run_restore(
            env_r,
            snap,
            env_name=env_name,
            skip_confirm=bool(ns.yes),
            config=config,
        )
    print(f"Unknown files subcommand: {sub!r}", file=sys.stderr)
    return 1


def _handle_bkp_db(
    ns: argparse.Namespace,
    config: ProjectConfig,
    env_name: str,
    env_add: dict[str, str],
    creds_path: Path,
    compose_file: str,
    docker_host: str,
    dry_run: bool,
    *,
    use_prepulled_registry: bool,
) -> int:
    sub = getattr(ns, "db_command", None) or getattr(ns, "bkp_db_command", None)
    bkp_tail: list[str] = []

    if sub and needs_pgbr_write(sub, bkp_tail) and not pgbr_write_configured(env_add):
        rc = ensure_spaces_backup_credentials(
            config,
            env_name,
            env_add,
            creds_path,
            target="pgbackrest",
            command_label=f"dk {env_name} db {sub}",
            dry_run=dry_run,
            yes=bool(ns.yes),
        )
        if rc != 0:
            return rc

    if sub == "init":
        rc = _ensure_local_stack_images_built(
            config,
            env_add,
            use_prepulled_registry=use_prepulled_registry,
        )
        if rc != 0:
            return rc
        img = postgres_image_from_env(env_add, config=config)
        print(f"Postgres image (volume ops / pgBackRest): {img}", file=sys.stderr)
        rc = run_bkp_db_init(
            compose_file, env_add, image=img, config=config, dk_env_name=env_name
        )
        if rc != 0:
            return rc
        if not getattr(ns, "install_systemd", False):
            return 0
        tail: list[str] = []
        if getattr(ns, "dry_run", False):
            tail.append("--dry-run")
        if getattr(ns, "enable", False):
            tail.append("--enable")
        return cmd_install_systemd_backups(
            config,
            env_add,
            str(docker_host),
            env_name,
            tail,
            global_dry_run=dry_run,
            yes=bool(ns.yes),
            fixed_only="pgbackrest",
        )

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
        rc = materialize_configs(env_add, dry_run=False, postgres_image=img, config=config)
        if rc != 0:
            return rc
        mode = getattr(ns, "configure_mode", None)
        if mode is None:
            return 0
        if mode == "verify":
            rc = run_pgbackrest_verify(env_add, image=img)
            if rc != 0:
                return rc
            return run_configure_verify_online_check(compose_file, env_add)
        if mode == "stanza-create":
            return run_bkp_db_stanza_create_flow(
                compose_file, env_add, image=img, config=config, dk_env_name=env_name
            )
        print(f"Unknown bkp_db configure mode: {mode!r}", file=sys.stderr)
        return 1

    if sub == "install-systemd":
        tail = []
        if getattr(ns, "dry_run", False):
            tail.append("--dry-run")
        if getattr(ns, "enable", False):
            tail.append("--enable")
        return cmd_install_systemd_backups(
            config,
            env_add,
            str(docker_host),
            env_name,
            tail,
            global_dry_run=dry_run,
            yes=bool(ns.yes),
            fixed_only="pgbackrest",
        )

    if sub == "restore":
        return run_unified_db_restore(
            config,
            compose_file=compose_file,
            env_add=env_add,
            env_name=env_name,
            force_dumps=bool(getattr(ns, "restore_from_dumps", False)),
            dry_run=dry_run or bool(getattr(ns, "restore_dry_run", False)),
            skip_confirm=bool(ns.yes),
            extra_pgbackrest_args=list(getattr(ns, "pgbackrest_restore_args", None) or []),
        )

    if sub == "backup":
        bt = ns.backup_type
        if not db_service_responds(compose_file, env_add):
            print(
                "The `db` service is not running on the deployment host. "
                "Start the stack before online backup.",
                file=sys.stderr,
            )
            return 1
        return run_pgbackrest_backup_online(compose_file, env_add, bt)

    if sub in ("info", "check", "version"):
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
        return run_pg_dump(compose_file, env_add, list(getattr(ns, "pg_dump_args", None) or []))

    if sub == "pgrestore":
        extras = list(getattr(ns, "pg_restore_args", None) or [])
        if getattr(ns, "archive_file", None):
            extras = ["--file", str(ns.archive_file), *extras]
        restore_extras = compose_pg_restore_extras_for_config(
            config,
            extras,
            default_archive=config.fetch_db_dump_path,
        )
        if "--file" not in restore_extras and sys.stdin.isatty():
            return 1
        rc = ensure_db_service_running(
            compose_file, env_add, config=config, dk_env_name=env_name
        )
        if rc != 0:
            return rc
        print(
            "bkp_db pgrestore: replacing app database with an empty database before restore …",
            file=sys.stderr,
        )
        rc = run_drop_create_app_database(
            compose_file,
            env_add,
            postgis=config.native.reset_db.postgis,
        )
        if rc != 0:
            return rc
        rc = run_pg_restore(compose_file, env_add, restore_extras, config=config)
        if rc != 0:
            return rc
        return run_post_db_restore_manage_commands(
            config,
            compose_file=compose_file,
            env_add=env_add,
            env_name=env_name,
        )

    print(f"Unknown db subcommand: {sub!r}", file=sys.stderr)
    return 1
