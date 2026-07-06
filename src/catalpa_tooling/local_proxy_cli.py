"""Top-level ``dk proxy`` command handlers."""

from __future__ import annotations

import argparse
import sys

from catalpa_tooling.local_proxy import (
    ensure_proxy_running,
    print_proxy_status,
    proxy_container_id,
    stop_proxy,
    wait_for_ca_root,
    wait_for_proxy_admin,
)
from catalpa_tooling.local_proxy_ca import print_proxy_ca_instructions
from catalpa_tooling.trust_caddy_cert import trust_caddy_ca_from_container


def cmd_proxy(ns: argparse.Namespace) -> int:
    sub = getattr(ns, "proxy_command", None)
    dry_run = bool(getattr(ns, "dry_run", False))

    if sub == "up":
        return ensure_proxy_running(dry_run=dry_run)

    if sub == "down":
        return stop_proxy(dry_run=dry_run)

    if sub == "status":
        print_proxy_status()
        return 0 if proxy_container_id() else 1

    if sub == "trust":
        if not dry_run:
            rc = ensure_proxy_running(dry_run=False)
            if rc != 0:
                return rc
            if not wait_for_ca_root():
                print(
                    "Timed out waiting for Caddy to generate its local CA. "
                    "Check `dk proxy status` and container logs.",
                    file=sys.stderr,
                )
                return 1
        return trust_caddy_ca_from_container(proxy_container_id(), dry_run=dry_run)

    if sub == "ca":
        if not dry_run:
            rc = ensure_proxy_running(dry_run=False)
            if rc != 0:
                return rc
            if not wait_for_proxy_admin():
                print(
                    "Timed out waiting for the proxy admin API. Check `dk proxy status`.",
                    file=sys.stderr,
                )
                return 1
        return print_proxy_ca_instructions(dry_run=dry_run)

    print(f"Unknown proxy subcommand: {sub!r}", file=sys.stderr)
    return 1
