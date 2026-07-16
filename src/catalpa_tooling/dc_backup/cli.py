"""``dk <env> dc-backup`` dispatcher."""

from __future__ import annotations

import sys
from typing import Any

from catalpa_tooling.config import ProjectConfig
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

    print(
        "usage: dk <env> dc-backup {tls,bootstrap,install,status}",
        file=sys.stderr,
    )
    return 2
