# Systemd backup timers on deploy hosts

Install rendered pgBackRest and restic timer units on the machine referenced by `docker_host` in `docker/envs/<env>/info.yaml` (typically `ssh://root@host`).

**Related docs**

- [README_PGBACKREST.md](README_PGBACKREST.md) — S3 credentials, `bkp_db configure`, prerequisites for pgBackRest timers
- [README_RESTIC.md](README_RESTIC.md) — restic repository credentials, `bkp_files init`, prerequisites for restic timers
- [ZABBIX_README.md](ZABBIX_README.md) — Zabbix Agent 2 (separate from backup timers; uses some of the same env files for monitoring)

## `tooling.yaml` configuration

Unit **filenames** are project-specific: `ops.systemd_unit_prefix` + a fixed template suffix. Paths in rendered units come from `ops.install_prefix` and `ops.config_dir`.

```yaml
ops:
  install_prefix: /opt/myapp      # backup shell scripts (pgbackrest-backup.sh, restic-files-backup.sh)
  config_dir: /etc/myapp          # EnvironmentFile= paths on the host
  systemd_unit_prefix: myapp-
  default_db_container: myapp_db_1   # fallback when auto-discovering Postgres container (pgBackRest only)
  systemd_units:
    pgbackrest:
      - myapp-pgbackrest-backup-full.service
      - myapp-pgbackrest-backup-incr.service
      - myapp-pgbackrest-backup-diff.service   # optional; install only if used
      - myapp-pgbackrest-backup-full.timer
      - myapp-pgbackrest-backup-incr.timer
      - myapp-pgbackrest-backup-diff.timer     # optional; see pgBackRest schedules below
    restic:
      - myapp-restic-files-backup.service
      - myapp-restic-files-backup.timer
    timers_enable_pgbackrest:
      - myapp-pgbackrest-backup-full.timer
      - myapp-pgbackrest-backup-incr.timer
    timers_enable_restic:
      - myapp-restic-files-backup.timer
```

See [tests/fixtures/indmo_reference_tooling.yaml](tests/fixtures/indmo_reference_tooling.yaml) for a full reference manifest.

## How units are rendered

Bundled templates live under `src/catalpa_tooling/systemd/templates/` with fixed suffixes:

| Template suffix | Role |
|-----------------|------|
| `pgbackrest-backup-full.service` | Oneshot full backup |
| `pgbackrest-backup-incr.service` | Oneshot incremental backup |
| `pgbackrest-backup-diff.service` | Oneshot differential backup |
| `pgbackrest-backup-full.timer` / `pgbackrest-backup-incr.timer` / `pgbackrest-backup-diff.timer` | Schedules |
| `restic-files-backup.service` | Oneshot media backup |
| `restic-files-backup.timer` | Schedule |

[`systemd_render.py`](src/catalpa_tooling/systemd_render.py) matches each name in `ops.systemd_units` to a template (longest suffix wins), checks that the name starts with `ops.systemd_unit_prefix`, and substitutes `@INSTALL_PREFIX@` and `@CONFIG_DIR@`.

### pgBackRest backup schedules

Default template calendars (UTC; adjust on the host or by forking templates):

| Timer | Default `OnCalendar` |
|-------|----------------------|
| full | `Sun *-*-* 03:15:00` |
| incr | `*-*-* 02:30:00` (daily) |
| diff | `Mon..Sat *-*-* 03:15:00` |

Common strategies:

- **Weekly full + daily incr** — enable `full` and `incr` timers only (default for small/medium DBs).
- **Weekly full + diff Mon–Sat** — enable `full` and `diff` timers; remove `incr` from `timers_enable_pgbackrest` ([pgBackRest user guide](https://pgbackrest.org/user-guide.html#schedule-a-backup)).
- **Hybrid** — e.g. full Sunday, diff Wednesday, incr other weekdays; customize `OnCalendar` in rendered timers or on the host.

Do **not** enable a daily **incr** timer and a daily **diff** timer on overlapping schedules unless you design a hybrid calendar explicitly — both would compete for the same backup chain.

When using differential backups in production, consider setting `repo1-retention-diff` (not yet rendered by `bkp_db configure`; see [README_PGBACKREST.md](README_PGBACKREST.md)).

## Install commands

From the **application repo root** (where `tooling.yaml` and `docker/envs/<env>/` live):

```bash
# pgBackRest timers (requires WRITE-mode pgbr_s3_write_* — see README_PGBACKREST.md)
dk prod bkp_db install-systemd --dry-run
dk prod bkp_db install-systemd --enable

# restic media timers (requires WRITE/legacy restic credentials — see README_RESTIC.md)
dk prod bkp_files install-systemd --dry-run
dk prod bkp_files install-systemd --enable
```

What gets installed on the remote host:

| Artifact | Location |
|----------|----------|
| `pgbackrest-backup.sh` / `restic-files-backup.sh` | `ops.install_prefix` |
| `pgbackrest-backup.env` / `restic-files-backup.env` | `ops.config_dir` |
| Rendered `.service` / `.timer` units | `/etc/systemd/system/` |

`--enable` runs `systemctl enable --now` on the timer names listed under `timers_enable_pgbackrest` or `timers_enable_restic`.

## SSH and `docker_host`

[`systemd_remote_install.py`](src/catalpa_tooling/systemd_remote_install.py) parses `docker_host` from `info.yaml` into an SSH target (`ssh://user@host` → `user@host`). Files are copied with `scp`; remote steps use `ssh`. The install machine needs SSH access to the deploy host and a working `docker` CLI there (timers exec into containers via the host Docker socket).

## Safety and flags

| Flag | Effect |
|------|--------|
| `--dry-run` | Print redacted env files and unit names; no `scp` or remote `systemctl` |
| `--enable` | Enable and start timer units after install |
| `--yes` | Allow `--enable` without a TTY (non-interactive CI) |
| Global `dk --dry-run` | Propagates to install-systemd where supported |

Without `--enable`, units are installed but timers are not started.

## Before you install timers

1. **pgBackRest:** Configure S3, materialize volume config, and verify backups — [README_PGBACKREST.md](README_PGBACKREST.md).
2. **restic:** Initialize the repository and run a manual backup — [README_RESTIC.md](README_RESTIC.md).
3. Ensure `docker_host` in `docker/envs/<env>/info.yaml` points at the target host.

Zabbix monitoring is installed separately: [ZABBIX_README.md](ZABBIX_README.md).
