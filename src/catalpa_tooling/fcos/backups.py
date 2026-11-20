"""Backups on an FCOS host: pgBackRest inside db, restic as a one-shot quadlet, timers.

The same keys as compose deploys: ``pgbr_s3_write_*`` and ``restic_write_*`` in the env's
``credentials.yaml``, ``ops.pgbackrest`` / ``ops.restic`` for what to back up, and
``ops.systemd_units`` for which timers run. The schedules are the bundled timer templates', so
both runtimes back up at the same times.

Compose deploys write the repository settings (bucket, path, stanza, …) into pgBackRest's
config. A render never decrypts credentials, so here every ``pgbr_s3_write_*`` and
``restic_write_*`` key reaches its container as a Podman secret, in the environment variable the
tool reads (pgBackRest takes any option as ``PGBACKREST_<OPTION>``; restic takes ``RESTIC_*`` and
``AWS_*``). The files keep only tuning, which is not secret: no credential is in a file, a
volume, or ``podman inspect``.

Only write mode is rendered: an FCOS host backs itself up. ``*_read_*`` keys (restoring another
env's backups) are left out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.fcos.quadlets import qualify_image, secret_name
from catalpa_tooling.fcos.settings import FcosSettings

PGBR_PREFIX = "PGBR_S3_WRITE_"
RESTIC_PREFIX = "RESTIC_WRITE_"

#: PGBR_S3_WRITE_<suffix> → the pgBackRest option it sets (same table as compose deploys).
_PGBR_OPTION = {
    "BUCKET": "repo1-s3-bucket",
    "REGION": "repo1-s3-region",
    "ENDPOINT": "repo1-s3-endpoint",
    "KEY": "repo1-s3-key",
    "SECRET": "repo1-s3-key-secret",
    "REPO_PATH": "repo1-path",
    "URI_STYLE": "repo1-s3-uri-style",
    "VERIFY_TLS": "repo1-storage-verify-tls",
    "STANZA": "stanza",
    "RETENTION_FULL": "repo1-retention-full",
}
_PGBR_REQUIRED = ("BUCKET", "REGION", "KEY", "SECRET", "REPO_PATH", "STANZA")

#: RESTIC_WRITE_<suffix> → the variable restic (or its S3 backend) reads.
_RESTIC_VAR = {
    "REPOSITORY": "RESTIC_REPOSITORY",
    "PASSWORD": "RESTIC_PASSWORD",
    "S3_ACCESS_KEY_ID": "AWS_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY",
    "S3_DEFAULT_REGION": "AWS_DEFAULT_REGION",
    "S3_SESSION_TOKEN": "AWS_SESSION_TOKEN",
}
_RESTIC_REQUIRED = ("REPOSITORY", "PASSWORD")


def pgbackrest_env_var(option: str) -> str:
    return "PGBACKREST_" + option.upper().replace("-", "_")


@dataclass
class BackupPlan:
    """What backups add before the quadlets are made (db's lines, restic's volumes) and what
    they need after (the ID ranges)."""

    pgbackrest: dict[str, str] = field(default_factory=dict)
    """Credential key → the variable it sets in db."""
    restic: dict[str, str] = field(default_factory=dict)
    restic_volumes: tuple[str, ...] = ()
    db_lines: tuple[str, ...] = ()
    problems: list[str] = field(default_factory=list)

    @property
    def secrets(self) -> dict[str, str]:
        """Podman secret → credential key."""
        return {secret_name(k): k for k in (*self.pgbackrest, *self.restic)}


def plan_backups(
    config: ProjectConfig,
    settings: FcosSettings,
    credential_keys: set[str],
    *,
    project: str,
    declared_volumes: set[str],
) -> BackupPlan:
    plan = BackupPlan()
    keys = {k.upper() for k in credential_keys}
    pg_keys = sorted(k for k in keys if k.startswith(PGBR_PREFIX))
    restic_keys = sorted(k for k in keys if k.startswith(RESTIC_PREFIX))
    if (pg_keys or restic_keys) and not config.has_ops:
        plan.problems.append("backup credentials are set but tooling.yaml has no `ops:`")
        return plan

    if pg_keys:
        for key in pg_keys:
            suffix = key[len(PGBR_PREFIX) :]
            if suffix not in _PGBR_OPTION:
                plan.problems.append(f"{key}: no pgBackRest option for `{suffix}`")
                continue
            plan.pgbackrest[key] = pgbackrest_env_var(_PGBR_OPTION[suffix])
        missing = [s for s in _PGBR_REQUIRED if f"{PGBR_PREFIX}{s}" not in keys]
        if missing:
            plan.problems.append(
                "pgBackRest: credentials.yaml lacks "
                + ", ".join(f"{PGBR_PREFIX}{s}".lower() for s in missing)
            )
        pg = config.ops.pgbackrest
        conf_dir = f"{settings.home}/.config/{project}"
        plan.db_lines = (
            *(
                f"Secret={secret_name(k)},type=env,target={var}"
                for k, var in sorted(plan.pgbackrest.items())
            ),
            # pg1-path is a stanza option; set here, since the config has no stanza section.
            f"Environment={pgbackrest_env_var('pg1-path')}={pg.pg1_path}",
            # Files deploy owns, so rootless Podman can relabel them (:Z). No secrets in them.
            f"Volume={conf_dir}/{pg.pgbackrest_conf}:/etc/pgbackrest/conf.d/{pg.pgbackrest_conf}:ro,Z",
            f"Volume={conf_dir}/{pg.postgres_conf}:/etc/postgresql/conf.d/{pg.postgres_conf}:ro,Z",
            # An init as PID 1. Async archive-push leaves a process behind; with postgres as
            # PID 1 it inherits it, and when that exits non-zero (the repository unreachable)
            # postgres takes it for a crashed backend and restarts in recovery mode.
            "RunInit=true",
        )
        if not _timer_kinds(config.ops.systemd_units.pgbackrest, "pgbackrest-backup-"):
            plan.problems.append(
                "pgBackRest credentials are set but ops.systemd_units.pgbackrest lists no "
                "pgbackrest-backup-{full,incr,diff}.timer"
            )

    if restic_keys:
        for key in restic_keys:
            suffix = key[len(RESTIC_PREFIX) :]
            if suffix not in _RESTIC_VAR:
                plan.problems.append(f"{key}: no restic variable for `{suffix}`")
                continue
            plan.restic[key] = _RESTIC_VAR[suffix]
        missing = [s for s in _RESTIC_REQUIRED if f"{RESTIC_PREFIX}{s}" not in keys]
        if missing:
            plan.problems.append(
                "restic: credentials.yaml lacks "
                + ", ".join(f"{RESTIC_PREFIX}{s}".lower() for s in missing)
            )
        volumes = config.ops.restic.data_volumes
        unknown = [v for v in volumes if v not in declared_volumes]
        plan.problems.extend(
            f"ops.restic.data_volumes: `{v}` is not a volume of the compose file" for v in unknown
        )
        plan.restic_volumes = tuple(v for v in volumes if v in declared_volumes)
        if not any(u.endswith("restic-files-backup.timer") for u in config.ops.systemd_units.restic):
            plan.problems.append(
                "restic credentials are set but ops.systemd_units.restic lists no "
                "restic-files-backup.timer"
            )
    return plan


def _timer_kinds(units: tuple[str, ...], stem: str) -> list[str]:
    return [kind for kind, _ in _pgbackrest_timers(units, stem)]


def _pgbackrest_timers(units: tuple[str, ...], stem: str = "pgbackrest-backup-") -> list[tuple[str, str]]:
    """(kind, timer name) for each pgBackRest timer ops.systemd_units lists, by its own name."""
    out = []
    for unit in units:
        m = re.search(rf"{re.escape(stem)}(full|incr|diff)\.timer$", unit)
        if m:
            out.append((m.group(1), unit))
    return out


def restic_unit_name(config: ProjectConfig) -> str | None:
    """The restic quadlet's name: the listed restic-files-backup timer's, so the timer starts it
    by name as on compose hosts."""
    for unit in config.ops.systemd_units.restic:
        if unit.endswith("restic-files-backup.timer"):
            return unit[: -len(".timer")]
    return None


def _template_schedule(template: str) -> str:
    from catalpa_tooling.systemd_render import systemd_templates_dir

    text = (systemd_templates_dir() / template).read_text(encoding="utf-8")
    m = re.search(r"^OnCalendar=(.+)$", text, re.MULTILINE)
    if not m:
        raise ValueError(f"no OnCalendar in the {template} template")
    return m.group(1).strip()


@dataclass
class BackupFiles:
    files: dict[str, str] = field(default_factory=dict)
    install: list[dict] = field(default_factory=list)
    timers: list[str] = field(default_factory=list)
    """User timers to enable."""


def _yn(env: dict[str, str], key: str, default: bool) -> str:
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return "y" if default else "n"
    return "y" if raw in ("1", "true", "yes", "y", "on") else "n"


def backup_files(
    plan: BackupPlan,
    config: ProjectConfig,
    settings: FcosSettings,
    *,
    header: str,
    project: str,
    compose_project: str,
    hostname: str,
    db_service: str,
    bind_volumes: tuple[str, ...],
    service_ranges: dict[str, int],
    writers: dict[str, list[str]],
    monitor_dir: str | None,
    env: dict[str, str],
) -> BackupFiles:
    """The backup files, now that every service's ID range is known."""
    from catalpa_tooling.fcos.settings import ID_RANGE_SIZE

    out = BackupFiles()
    if not (plan.pgbackrest or plan.restic):
        return out
    ops = config.ops
    prefix = ops.systemd_unit_prefix
    conf_dir = f"{settings.home}/.config/{project}"
    status_unit = f"{prefix}backup-status.service" if monitor_dir else None
    status_post = (
        f"# Refresh the status Zabbix reads. --no-block: don't wait for it.\n"
        f"ExecStartPost=/usr/bin/systemctl --user start --no-block {status_unit}\n"
        if status_unit
        else ""
    )

    def add(path: str, text: str, dest: str, owner: str, mode: str = "0644") -> None:
        out.files[path] = text
        out.install.append({"file": path, "dest": dest, "owner": owner, "mode": mode})

    if plan.pgbackrest:
        pg = ops.pgbackrest
        add(
            "backup/pgbackrest.conf",
            header
            + "# Mounted into db as /etc/pgbackrest/conf.d/"
            + pg.pgbackrest_conf
            + ". Tuning only: the repository\n"
            "# (bucket, path, stanza, key) comes from db's Podman secrets as PGBACKREST_* variables.\n"
            "[global]\n"
            "repo1-type=s3\n"
            f"repo1-bundle={_yn(env, 'PGBR_REPO1_BUNDLE', True)}\n"
            f"repo1-block={_yn(env, 'PGBR_REPO1_BLOCK', True)}\n"
            f"process-max={env.get('PGBR_PROCESS_MAX') or '2'}\n"
            f"archive-async={_yn(env, 'PGBR_ARCHIVE_ASYNC', True)}\n"
            f"repo1-retention-full-type={env.get('PGBR_REPO1_RETENTION_FULL_TYPE') or 'time'}\n"
            f"repo1-retention-full={env.get('PGBR_REPO1_RETENTION_FULL') or '30'}\n"
            "\n"
            "[global:archive-push]\n"
            f"compress-level={env.get('PGBR_COMPRESS_LEVEL') or '3'}\n",
            f"{conf_dir}/{pg.pgbackrest_conf}",
            settings.user,
        )
        add(
            "backup/postgres-archive.conf",
            header
            + f"# Mounted into db as /etc/postgresql/conf.d/{pg.postgres_conf}. archive_command runs\n"
            "# as postgres and inherits db's environment, so the stanza and repository come from\n"
            "# the PGBACKREST_* secrets.\n"
            "wal_level = replica\n"
            "archive_mode = on\n"
            "archive_command = 'pgbackrest archive-push %p'\n",
            f"{conf_dir}/{pg.postgres_conf}",
            settings.user,
        )
        timers = _pgbackrest_timers(ops.systemd_units.pgbackrest)
        # The listed names, as on compose hosts: <stem>-full.timer starts <stem>@full.service.
        stem = timers[0][1][: -len(f"-{timers[0][0]}.timer")]
        service = f"{stem}@.service"
        add(
            f"systemd/{service}",
            header
            + f"# pgBackRest backup of type %i (full, incr or diff) inside {db_service}. By hand:\n"
            f"#   systemctl --user start {stem}@full.service\n"
            "[Unit]\n"
            "Description=pgBackRest %i backup\n"
            f"Requires={db_service}.service\n"
            f"After={db_service}.service\n"
            "\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"ExecStart=/usr/bin/podman exec -u postgres {db_service} pgbackrest "
            "--log-level-console=info backup --type=%i\n" + status_post,
            f"{settings.user_unit_dir}/{service}",
            settings.user,
        )
        for kind, timer in timers:
            add(
                f"systemd/{timer}",
                header
                + f"# Schedule from dk's pgbackrest-backup-{kind}.timer template (the host's time zone).\n"
                "[Unit]\n"
                f"Description=pgBackRest {kind} backup\n"
                "\n"
                "[Timer]\n"
                f"OnCalendar={_template_schedule(f'pgbackrest-backup-{kind}.timer')}\n"
                "Persistent=true\n"
                f"Unit={stem}@{kind}.service\n"
                "\n"
                "[Install]\n"
                "WantedBy=timers.target\n",
                f"{settings.user_unit_dir}/{timer}",
                settings.user,
            )
            if timer in ops.systemd_units.timers_enable_pgbackrest:
                out.timers.append(timer)

    if plan.restic:
        volumes = plan.restic_volumes
        ranges = set()
        for v in volumes:
            users = writers.get(v) or []
            ranges |= {service_ranges[u] for u in users if u in service_ranges}
        start = min(ranges) if ranges else None
        if len(ranges) > 1:
            raise ValueError(
                "ops.restic.data_volumes are written by services in different ID ranges "
                f"({', '.join(map(str, sorted(ranges)))}); restic can read only one range's files"
            )
        name = restic_unit_name(config) or f"{prefix}restic-files-backup"
        mounts = []
        paths = []
        on_data = False
        for i, v in enumerate(volumes):
            target = (ops.restic.backup_path if i == 0 and ops.restic.backup_path else f"/backup/{v}")
            paths.append(target)
            if v in bind_volumes:
                mounts.append(f"Volume={settings.data_mount}/{v}:{target}:ro,z")
                on_data = True
            else:
                mounts.append(f"Volume={v}.volume:{target}:ro,z")
        secret_lines = [
            f"Secret={secret_name(k)},type=env,target={var}" for k, var in sorted(plan.restic.items())
        ]
        from catalpa_tooling.restic_files import RESTIC_IMAGE

        image = qualify_image(RESTIC_IMAGE)
        unit = [
            "[Unit]",
            f"Description=restic backup of {', '.join(volumes)}",
            "Wants=network-online.target",
            "After=network-online.target",
        ]
        if on_data:
            unit.append(f"RequiresMountsFor={settings.data_mount}")
        container = [
            "",
            "[Container]",
            f"ContainerName={name}",
            f"Image={image}",
        ]
        if start is not None:
            # The range of the services that write these files: root inside owns them, and the
            # read-only mounts keep restic from changing them.
            container += [f"UIDMap=0:{start}:{ID_RANGE_SIZE}", f"GIDMap=0:{start}:{ID_RANGE_SIZE}"]
        container += secret_lines + mounts
        container += [
            "# restic's cache, so each run doesn't download the repository index again.",
            f"Volume={compose_project}_restic_cache:/root/.cache/restic",
            "# A fixed --host: the container's own hostname changes every run, so restic would",
            "# find no parent snapshot and `forget` couldn't group snapshots.",
            f"Exec=backup --host {hostname} {' '.join(paths)}",
            "# Output once, as the unit's (journalctl -u), through podman's attached stdout: a",
            "# one-shot needs no `podman logs`. Podman's journald driver logged every line twice,",
            "# and passthrough logs as the container's ID-range uid, which the account can't read.",
            "LogDriver=none",
        ]
        service = ["", "[Service]", "Type=oneshot", "TimeoutStartSec=3600"]
        text = header + "\n".join(unit + container + service) + "\n" + status_post
        add(
            f"quadlets/{name}.container",
            text,
            f"{settings.quadlet_dir}/{name}.container",
            "root",
        )
        for unit_name in ops.systemd_units.restic:
            if not unit_name.endswith("restic-files-backup.timer"):
                continue
            add(
                f"systemd/{unit_name}",
                header
                + "# Schedule from dk's restic-files-backup.timer template (the host's time zone).\n"
                "[Unit]\n"
                f"Description=restic backup of {', '.join(volumes)}\n"
                "\n"
                "[Timer]\n"
                f"OnCalendar={_template_schedule('restic-files-backup.timer')}\n"
                "Persistent=true\n"
                f"Unit={name}.service\n"
                "\n"
                "[Install]\n"
                "WantedBy=timers.target\n",
                f"{settings.user_unit_dir}/{unit_name}",
                settings.user,
            )
            if unit_name in ops.systemd_units.timers_enable_restic:
                out.timers.append(unit_name)

    if monitor_dir:
        _status_files(out, plan, settings, header, prefix, conf_dir, db_service, monitor_dir)
    return out


def _status_files(out, plan, settings, header, prefix, conf_dir, db_service, monitor_dir) -> None:
    from catalpa_tooling.restic_files import RESTIC_IMAGE

    parts = [
        "#!/bin/sh",
        header.rstrip("\n"),
        "# Backup status as JSON for the Zabbix agent, which only reads these files: it holds no",
        "# credentials and execs nothing. Each file is replaced only on success, so Zabbix should",
        "# also alert on the files' age.",
        "set -u",
        f"D={monitor_dir}",
        "rc=0",
    ]
    if plan.pgbackrest:
        parts += [
            "",
            f"if podman exec -u postgres {db_service} pgbackrest --output=json info > \"$D/.pgbackrest-info.json\"; then",
            '  mv "$D/.pgbackrest-info.json" "$D/pgbackrest-info.json"',
            "else",
            '  echo "backup-status: pgbackrest info failed" >&2',
            "  rc=1",
            "fi",
        ]
    if plan.restic:
        secrets = " ".join(
            f"--secret {secret_name(k)},type=env,target={var}" for k, var in sorted(plan.restic.items())
        )
        parts += [
            "",
            "# --no-cache: listing snapshots is cheap, and the cache volume is restic's own range.",
            f"if podman run --rm {secrets} \\",
            f"    {qualify_image(RESTIC_IMAGE)} --no-cache --json snapshots > \"$D/.restic-snapshots.json\"; then",
            '  mv "$D/.restic-snapshots.json" "$D/restic-snapshots.json"',
            "else",
            '  echo "backup-status: restic snapshots failed" >&2',
            "  rc=1",
            "fi",
        ]
    parts += ["", "exit $rc"]
    script = f"{conf_dir}/backup-status.sh"
    out.files["backup/backup-status.sh"] = "\n".join(parts) + "\n"
    out.install.append(
        {"file": "backup/backup-status.sh", "dest": script, "owner": settings.user, "mode": "0755"}
    )
    service = f"{prefix}backup-status.service"
    timer = f"{prefix}backup-status.timer"
    for path, text in (
        (
            service,
            "[Unit]\nDescription=Write backup status for Zabbix\n"
            f"After={db_service}.service\n\n[Service]\nType=oneshot\nExecStart={script}\n",
        ),
        (
            timer,
            "# Hourly, so Zabbix sees WAL archiving between backups, and the files' age shows\n"
            "# whether this job runs.\n"
            "[Unit]\nDescription=Hourly backup status for Zabbix\n\n"
            "[Timer]\nOnCalendar=hourly\nRandomizedDelaySec=120\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n",
        ),
    ):
        out.files[f"systemd/{path}"] = header + text
        out.install.append(
            {
                "file": f"systemd/{path}",
                "dest": f"{settings.user_unit_dir}/{path}",
                "owner": settings.user,
                "mode": "0644",
            }
        )
    out.timers.append(timer)
