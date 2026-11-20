"""Backup credentials for a ``runtime: quadlet`` env, created when missing.

The same DigitalOcean Spaces provisioning as compose envs (``doctl_spaces_provision``): when
``credentials.yaml`` has no WRITE credentials for pgBackRest (``pgbr_s3_write_*``) or restic
(``restic_write_*``), ``db configure stanza-create``, ``db backup``, ``files init`` and
``files backup`` offer to create the bucket and a key scoped to it, and write them with
``sops set``.

A quadlet env's containers read those values as Podman secrets, not from the file. So the
secrets whose values changed are then pushed to the host, and the long-running units that
use them restarted (``db``, whose ``archive_command`` reads the repository from its
environment). One-shot units (the restic backup) read them on their next run.
"""

from __future__ import annotations

import shlex
import sys
from typing import Literal

from catalpa_tooling.fcos import host as h

Target = Literal["pgbackrest", "restic"]


def _flat(credentials: dict) -> dict[str, str]:
    return {
        str(k).upper(): str(v)
        for k, v in credentials.items()
        if k != "sops" and not isinstance(v, dict | list) and v is not None
    }


def needs_write(verb: str, sub: str | None, ns) -> Target | None:
    """Which WRITE credentials ``dk <env> <verb> <sub>`` needs, if any."""
    if verb == "db" and (sub == "backup" or (sub == "configure" and getattr(ns, "configure_mode", None) == "stanza-create")):
        return "pgbackrest"
    if verb == "files" and sub in ("init", "backup"):
        return "restic"
    return None


def ensure_backup_credentials(
    b, target: Target, *, yes: bool, dry_run: bool, label: str, read_credentials=None
) -> int:
    """0 when the credentials are (now) in place and on the host. ``b``: a ``Backend``."""
    from catalpa_tooling.doctl_spaces_provision import (
        ensure_spaces_backup_credentials,
        pgbr_write_configured,
        restic_write_configured,
    )

    if read_credentials is None:
        from catalpa_tooling.fcos.cli import _credentials

        def read_credentials():
            return _credentials(b.config, b.env_name)

    before = read_credentials()
    env_add = _flat(before)
    configured = pgbr_write_configured if target == "pgbackrest" else restic_write_configured
    if configured(env_add):
        return 0
    creds_path = b.config.deploy_envs_dir / b.env_name / "credentials.yaml"
    rc = ensure_spaces_backup_credentials(
        b.config, b.env_name, env_add, creds_path,
        target=target, command_label=label, dry_run=dry_run, yes=yes,
    )  # fmt: skip
    if rc != 0 or dry_run:
        return rc

    after = _flat(read_credentials())
    changed = {k for k, v in after.items() if _flat(before).get(k) != v}
    c = b.committed
    names = sorted(n for n, key in (c.manifest.get("secrets") or {}).items() if key.upper() in changed)
    rendered = {key.upper() for key in (c.manifest.get("secrets") or {}).values()}
    if changed - rendered:
        # The committed render predates these credentials, so it has no backup units for
        # them: the render switches backups on by which credentials exist.
        print(
            f"{label}: the credentials are created, but the committed render has no backups for "
            f"them yet. Run `dk {b.env_name} render`, commit, `dk {b.env_name} secrets "
            f"push`, then a bare `dk {b.env_name}` to apply it, and run this again.",
            file=sys.stderr,
        )
        return 1
    values, problems = h.secret_values(c, read_credentials(), names)
    if problems:
        for p in problems:
            print(f"{label}: {p}", file=sys.stderr)
        return 1
    print(f"{b.env_name}: pushing the new secrets to the host …", file=sys.stderr)
    h.push_secrets(b.host, c, values)
    units = h.restarts_for(c, [], secrets=names).user
    if units:
        print(f"{b.env_name}: systemctl --user restart {' '.join(units)}", file=sys.stderr)
        rc = b._ssh("systemctl --user restart " + " ".join(shlex.quote(u) for u in units), tty=False)
        if rc != 0:
            return rc
    print(
        f"Commit {creds_path.relative_to(b.config.repo_root)}: it has the new credentials.",
        file=sys.stderr,
    )
    return 0
