"""What an env does with backups, from ``info.yaml`` (#204, tooling 2.0):

```yaml
backups: [db, files]   # this env backs itself up: db = pgBackRest, files = restic
restore_from: prod     # this env restores prod's backups
```

These settings decide behaviour; the credentials only have to match them. ``backups`` lists
the kinds this env backs up, which need write credentials (``pgbr_s3_write_*`` for db,
``restic_write_*`` for files). ``restore_from`` names the env whose backups this one restores,
which needs read credentials (``pgbr_s3_read_*`` / ``restic_read_*``), for the kinds the source
lists under its own ``backups``.

Absent means off: an env without ``backups`` takes no backups and gets no timers. Credentials
without their setting are an error rather than ignored, so a prod env upgraded without
``backups:`` stops with a hint instead of quietly losing its backups. A setting without its
credentials is not an error here: the commands that need them say how to make them
(``db configure stanza-create`` / ``files init`` for write, ``secrets read-key`` for read).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import yaml

from catalpa_tooling.config import ProjectConfig

#: ``backups`` entry → (tool, write prefix, read prefix), prefixes upper-case as in the env.
KINDS: dict[str, tuple[str, str, str]] = {
    "db": ("pgBackRest", "PGBR_S3_WRITE_", "PGBR_S3_READ_"),
    "files": ("restic", "RESTIC_WRITE_", "RESTIC_READ_"),
}
#: restic's pre-prefix names, still read as write credentials.
_LEGACY_RESTIC = ("RESTIC_REPOSITORY", "RESTIC_PASSWORD")


class BackupSettingsError(ValueError):
    pass


@dataclass(frozen=True)
class BackupSettings:
    env: str
    backups: frozenset[str]
    """Kinds this env backs up."""
    restore_from: str | None = None
    restores: frozenset[str] = frozenset()
    """Kinds it restores from ``restore_from`` (what the source backs up)."""

    def writes(self, kind: str) -> bool:
        return kind in self.backups

    def reads(self, kind: str) -> bool:
        return kind in self.restores


def _parse_backups(env: str, raw: Any) -> frozenset[str]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or not all(isinstance(k, str) for k in raw):
        raise BackupSettingsError(f"{env}: backups: is a list of kinds, e.g. `backups: [db, files]`")
    unknown = [k for k in raw if k not in KINDS]
    if unknown:
        raise BackupSettingsError(f"{env}: backups: unknown {', '.join(unknown)} (kinds: db, files)")
    return frozenset(raw)


def load_info(config: ProjectConfig, env: str) -> dict:
    path = config.deploy_envs_dir / env / "info.yaml"
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def backup_settings(config: ProjectConfig, env: str, info: dict[str, Any]) -> BackupSettings:
    from catalpa_tooling.restore_from import RestoreFromError, restore_source

    backups = _parse_backups(env, info.get("backups"))
    try:
        source = restore_source(config, env, info)
    except RestoreFromError as exc:
        raise BackupSettingsError(str(exc)) from exc
    restores: frozenset[str] = frozenset()
    if source:
        restores = _parse_backups(source, load_info(config, source).get("backups"))
        if not restores:
            raise BackupSettingsError(
                f"{env}: restore_from: {source}, but {source} backs nothing up "
                f"(no backups: in docker/envs/{source}/info.yaml)"
            )
    return BackupSettings(env=env, backups=backups, restore_from=source, restores=restores)


def credential_kinds(names: Iterable[str]) -> tuple[set[str], set[str]]:
    """(kinds with write credentials, kinds with read credentials) among ``names``."""
    upper = {n.upper() for n in names}
    write, read = set(), set()
    for kind, (_tool, w, r) in KINDS.items():
        if any(n.startswith(w) for n in upper):
            write.add(kind)
        if any(n.startswith(r) for n in upper):
            read.add(kind)
    if any(n in upper for n in _LEGACY_RESTIC):
        write.add("files")
    return write, read


def credential_problems(s: BackupSettings, names: Iterable[str]) -> list[str]:
    """Credentials that don't match the settings (credentials without their setting)."""
    write, read = credential_kinds(names)
    out = []
    for kind in sorted(write - s.backups):
        tool, prefix, _r = KINDS[kind]
        out.append(
            f"{s.env} has {tool} write credentials ({prefix.lower()}*) but doesn't back up {kind}: "
            f"add `backups: [{', '.join(sorted(s.backups | {kind}))}]` to its info.yaml if it should, "
            "or remove the credentials"
        )
    for kind in sorted(read - s.restores):
        tool, _w, prefix = KINDS[kind]
        why = (
            f"restore_from: {s.restore_from} doesn't back up {kind}"
            if s.restore_from
            else "it has no restore_from"
        )
        out.append(
            f"{s.env} has {tool} read credentials ({prefix.lower()}*) but {why}: set `restore_from: "
            "<env>` in its info.yaml to the env whose backups they read, or remove them"
        )
    return out


def refuse_write(s: BackupSettings, kind: str, what: str) -> str | None:
    """Why ``what`` (a command that backs up) doesn't run here, or None when it may."""
    if s.writes(kind):
        return None
    tool = KINDS[kind][0]
    src = f" It restores {s.restore_from}'s backups instead." if s.reads(kind) else ""
    return (
        f"{what}: {s.env} doesn't back up {kind} ({tool}).{src} To have it back up, add "
        f"`backups: [{', '.join(sorted(s.backups | {kind}))}]` to docker/envs/{s.env}/info.yaml."
    )
