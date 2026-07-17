# Zabbix Agent 2 on deploy hosts

Install and manage [Zabbix Agent 2](https://www.zabbix.com/documentation/current/en/manual/installation/containers) as a Docker container under systemd on the machine referenced by `docker_host` in `docker/envs/<env>/info.yaml` (app), or on `dc_backup_docker_host` with `--target backup`.

**Related docs**

- [README_PGBACKREST.md](README_PGBACKREST.md) — `pgbackrest.info` UserParameter (docker exec into db)
- [README_RESTIC.md](README_RESTIC.md) — `restic.snapshots` UserParameter (uses `restic-files-backup.env`)
- [README_DC_BACKUP.md](README_DC_BACKUP.md) — Garage / offsite UserParameters on the backup host
- [README.md](README.md) — main tooling overview

## Commands

From the **application repo root**:

```bash
dk prod zabbix [--target app|backup] install [--server HOST] [--hostname NAME] [--dry-run] [--force]
dk prod zabbix [--target app|backup] enable [--dry-run]
dk prod zabbix [--target app|backup] disable [--dry-run]
dk prod zabbix [--target app|backup] restart [--dry-run]
dk prod zabbix [--target app|backup] logs [-n N] [-f]
```

`--target app` (default) uses `docker_host`. `--target backup` uses `dc_backup_docker_host` and installs backup-oriented UserParameters (`garage.status`, `dc-backup.offsite.timer`). Hostname for backup must be set with `--hostname` or `zbx_hostname_backup` (not app `zbx_hostname` / `site_origin`).

When the chosen host is an SSH URL, these run on the remote host via SSH. If `docker_host` targets the local engine (`--target app` only), commands run locally.

`install` writes:

| File | Purpose |
|------|---------|
| `/etc/systemd/system/<ops.zabbix.unit_name>` | systemd unit (default name from `tooling.yaml`) |
| `<ops.config_dir>/zabbix-agent2.docker.env` | `ZBX_*` for `docker run --env-file` |
| `<ops.config_dir>/zabbix-agent2-userparams.conf` | `UserParameter` lines for the agent |

Then `systemctl daemon-reload`. Use `enable` to `systemctl enable --now` the unit.

## `tooling.yaml`

```yaml
ops:
  config_dir: /etc/myapp
  zabbix:
    unit_name: myapp-zabbix-agent2.service
    userparams_file: 99-myapp-userparams.conf   # filename inside agent conf.d
```

`config_dir` is shared with backup env files ([README_SYSTEMD.md](README_SYSTEMD.md)).

## Configuration sources

Precedence for install (CLI wins, then env file defaults):

1. **CLI:** `--server`, `--hostname`, `--active-allow` / `--no-active-allow`
2. **`info.yaml`:** keys under `env:` or top-level `zbx_*` (mapped to `ZBX_*`)
3. **`credentials.yaml`:** any `ZBX_*` keys merged into the agent env file
4. **Hostname fallback:** host part of the **primary** `site_origin` in `info.yaml` (first entry when `site_origin` is a list) when `--hostname` is omitted

`install` refuses to run without at least one `ZBX_*` source unless you pass `--force`.

### Common `info.yaml` keys (lowercase → uppercase)

| Key | Maps to | Notes |
|-----|---------|-------|
| `zbx_server_host` | `ZBX_SERVER_HOST` | Zabbix server or proxy |
| `zbx_hostname` | `ZBX_HOSTNAME` | Must match host object in Zabbix (**app** target) |
| `zbx_hostname_backup` | `ZBX_HOSTNAME_BACKUP` | Hostname for **`--target backup`** (required unless `--hostname`) |
| `zbx_active_allow` | `ZBX_ACTIVE_ALLOW` | `true` / `false` |
| `zbx_metadata` | `ZBX_METADATA` | Host metadata; install always appends `docker` plus `app` or `backup` from `--target` (e.g. `kafemalirin` → `kafemalirin docker app`) |
| `zabbix_userparameter_pgbackrest` | `ZABBIX_USERPARAMETER_PGBACKREST` | Default `true`; set `false` to omit `pgbackrest.info` (app only) |
| `zabbix_userparameter_restic` | `ZABBIX_USERPARAMETER_RESTIC` | Default `true`; set `false` to omit `restic.snapshots` (app only) |
| `zabbix_docker_db_container` | `ZABBIX_DOCKER_DB_CONTAINER` | Override db container name for UserParameter |
| `compose_project_name` | Used to resolve default db container name | With `zabbix_compose_db_service` (default `db`) |

TLS PSK secrets (`zbx_tlspsk`, `zbx_tlspskidentity`) must live in **`credentials.yaml`**, not `info.yaml`. If they appear only in `info.yaml`, `dk` warns and expects them under credentials as `ZBX_TLSPSK` / `ZBX_TLSPSKIDENTITY`.

### Example `info.yaml` fragment

```yaml
# site_origin may be a string or YAML list (hostnames or full URLs); Zabbix uses the first entry.
site_origin:
  - https://myapp.example.org
  - www.myapp.example.org
env:
  zbx_server_host: zabbix.catalpa.build
  zbx_hostname: myapp.example.org
  zbx_active_allow: true
  zabbix_userparameter_pgbackrest: true
  zabbix_userparameter_restic: true
```

## UserParameters

Generated into `zabbix-agent2-userparams.conf` on install ([`zabbix_systemd.py`](src/catalpa_tooling/zabbix_systemd.py)):

**`--target app` (default)**

| Key | Command summary |
|-----|-----------------|
| `pgbackrest.info` | `docker exec` into Postgres container as `postgres`: `pgbackrest info --output=json` |
| `restic.snapshots` | `docker run --rm` with `--env-file` = `restic-files-backup.env`, `restic --json snapshots` |

**`--target backup`**

| Key | Command summary |
|-----|-----------------|
| `garage.status` | `docker exec garage /garage status` |
| `dc-backup.offsite.timer` | `systemctl show -p ActiveState --value <ops.systemd_unit_prefix>rclone-garage-offsite.timer` |

Agent commands use `chroot /host/root` plus the **host** Docker CLI (the agent image is Alpine/musl and cannot run the host `docker` binary directly). The unit bind-mounts the host root and Docker socket.

Optional env overrides:

- `ZABBIX_RESTIC_DOCKER_ENV_FILE` — default `<config_dir>/restic-files-backup.env` (install restic systemd first: [README_RESTIC.md](README_RESTIC.md))
- `ZABBIX_DOCKER_DB_CONTAINER` — explicit container name
- `ZABBIX_CHROOT_DOCKER_CLI` — host docker binary path (default `/usr/bin/docker`)

## Prerequisites

1. Deploy host with Docker and SSH access from your workstation (same as `dk prod up`).
2. For **pgbackrest.info:** running `db` container and working pgBackRest config ([README_PGBACKREST.md](README_PGBACKREST.md)).
3. For **restic.snapshots:** `restic-files-backup.env` on the host (from `bkp_files install-systemd` or manual copy).
4. For **`--target backup`:** `dc_backup_docker_host`, Garage container named `garage`, and (for the timer item) offsite units from `dc-backup offsite install`.

## Example workflow

```bash
dk prod zabbix install --dry-run
dk prod zabbix install --hostname myapp.example.org
dk prod zabbix enable
dk prod zabbix logs -n 50

# Garage / backup host (separate Zabbix host object):
dk prod zabbix --target backup install --hostname myapp-dc-backup
dk prod zabbix --target backup enable
dk prod zabbix --target backup restart
```

Default agent image: `zabbix/zabbix-agent2:alpine-latest` (override with `install --image`).

After changing `info.yaml` compose or UserParameter toggles, re-run `install` and `restart`.
