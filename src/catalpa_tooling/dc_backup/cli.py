"""``dk <env> dc-backup`` dispatcher."""

from __future__ import annotations

import sys
from typing import Any

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dc_backup.offsite import (
    cmd_dc_backup_offsite_install,
    cmd_dc_backup_offsite_run,
    cmd_dc_backup_offsite_status,
)
from catalpa_tooling.dc_backup.provision import cmd_dc_backup_provision
from catalpa_tooling.dc_backup.stack import (
    cmd_dc_backup_bootstrap,
    cmd_dc_backup_install,
    cmd_dc_backup_status,
)
from catalpa_tooling.dc_backup.tls import (
    cmd_dc_backup_tls_install,
    cmd_dc_backup_tls_issue,
    cmd_dc_backup_tls_status,
)


def handle_dc_backup_command(
    ns: Any,
    config: ProjectConfig,
    env_name: str,
    *,
    dry_run: bool,
) -> int:
    """Dispatch ``dk <env> dc-backup …``."""
    sub = getattr(ns, "dc_backup_command", None)

    if sub == "tls":
        tls_sub = getattr(ns, "dc_backup_tls_command", None)
        if tls_sub == "issue":
            return cmd_dc_backup_tls_issue(
                config,
                env_name,
                ips=list(getattr(ns, "ips", None) or []),
                dns_names=list(getattr(ns, "dns_names", None) or []),
                days=int(getattr(ns, "days", 825) or 825),
                force=bool(getattr(ns, "force", False)),
                dry_run=dry_run,
            )
        if tls_sub == "install":
            return cmd_dc_backup_tls_install(config, env_name, dry_run=dry_run)
        if tls_sub == "status":
            return cmd_dc_backup_tls_status(
                config,
                env_name,
                check_remote=bool(getattr(ns, "check_remote", False)),
            )
        print(
            "usage: dk <env> dc-backup tls {issue,install,status}",
            file=sys.stderr,
        )
        return 2

    if sub == "bootstrap":
        return cmd_dc_backup_bootstrap(
            config,
            env_name,
            force=bool(getattr(ns, "force", False)),
            dry_run=dry_run,
        )
    if sub == "install":
        return cmd_dc_backup_install(
            config,
            env_name,
            up=bool(getattr(ns, "up", False)),
            dry_run=dry_run,
        )
    if sub == "status":
        return cmd_dc_backup_status(
            config,
            env_name,
            check_remote=bool(getattr(ns, "check_remote", False)),
        )
    if sub == "provision":
        return cmd_dc_backup_provision(
            config,
            env_name,
            dry_run=dry_run or bool(getattr(ns, "dc_backup_provision_dry_run", False)),
            yes=bool(getattr(ns, "yes", False)),
            print_only=bool(getattr(ns, "print_only", False)),
            force=bool(getattr(ns, "force", False)),
            bucket=getattr(ns, "bucket", None),
            key_name=getattr(ns, "key_name", None),
            endpoint=getattr(ns, "endpoint", None),
            pgbr_repo_path=getattr(ns, "pgbr_repo_path", None),
            restic_prefix=getattr(ns, "restic_prefix", None),
            capacity=getattr(ns, "capacity", None),
        )
    if sub == "offsite":
        offsite_sub = getattr(ns, "dc_backup_offsite_command", None)
        if offsite_sub == "install":
            return cmd_dc_backup_offsite_install(
                config,
                env_name,
                enable=bool(getattr(ns, "enable", False)),
                yes=bool(getattr(ns, "yes", False)),
                dry_run=dry_run or bool(getattr(ns, "dc_backup_offsite_dry_run", False)),
            )
        if offsite_sub == "run":
            return cmd_dc_backup_offsite_run(
                config,
                env_name,
                dry_run=dry_run or bool(getattr(ns, "dc_backup_offsite_dry_run", False)),
            )
        if offsite_sub == "status":
            return cmd_dc_backup_offsite_status(config, env_name)
        print(
            "usage: dk <env> dc-backup offsite {install,run,status}",
            file=sys.stderr,
        )
        return 2

    print(
        "usage: dk <env> dc-backup {tls,bootstrap,install,status,provision,offsite}",
        file=sys.stderr,
    )
    return 2
