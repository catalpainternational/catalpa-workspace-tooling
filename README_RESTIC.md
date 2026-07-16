# Restic (media files, `bkp_files`)

Back up the stack’s **`django_media`** Docker volume with [restic](https://restic.net) via `docker run restic/restic`. Repository credentials live in `docker/envs/<env>/credentials.yaml` in the consumer app repo.

**Related docs**

- [README_SYSTEMD.md](README_SYSTEMD.md) — install `*-restic-files-backup.timer` on the deploy host
- [ZABBIX_README.md](ZABBIX_README.md) — `restic.snapshots` UserParameter (reads `restic-files-backup.env`)
- [README_DC_BACKUP.md](README_DC_BACKUP.md) — private CA mount, `DOCKER_ADD_HOST`, `dk dc-backup`
- [README.md](README.md) — main tooling overview

## What gets backed up

Restic snapshots the named volume `{COMPOSE_PROJECT_NAME}_{data_volume}` (default `data_volume` = `django_media`). The backup script mounts it at `/backup/{data_volume}` inside the restic container ([`restic_files.py`](src/catalpa_tooling/restic_files.py)).

Configure the compose volume key in `tooling.yaml`:

```yaml
ops:
  restic:
    data_volume: django_media   # optional; default django_media
    backup_path: /mnt/btrfs-data/myapp-media   # optional; path prefix in snapshots
```

When ``backup_path`` is set, the compose volume is mounted at that path inside the restic container for backup and restore (must match paths in existing snapshots). Default: ``/backup/{data_volume}``.

`COMPOSE_PROJECT_NAME` must match the Compose project on the deploy host (from `info.yaml` env or `stack.compose_project_default` in `tooling.yaml`). The volume key must match the `volumes:` name in your compose file.

## Credentials per environment

Use **one** credential style per environment (mutually exclusive). Keys in `credentials.yaml` are uppercased by `dk` like other deploy secrets.

### WRITE mode (scheduled backup hosts)

Prefix keys with `restic_write_` → `RESTIC_WRITE_*`, which normalize to canonical `RESTIC_*` for restic:

| Suffix | Canonical env |
|--------|----------------|
| `repository` | `RESTIC_REPOSITORY` |
| `password` | `RESTIC_PASSWORD` |
| `s3_access_key_id` | `RESTIC_S3_ACCESS_KEY_ID` |
| `s3_secret_access_key` | `RESTIC_S3_SECRET_ACCESS_KEY` |
| `s3_default_region` | `RESTIC_S3_DEFAULT_REGION` |
| `s3_session_token` | `RESTIC_S3_SESSION_TOKEN` |

### READ mode (restore / audit hosts)

`restic_read_*` → `RESTIC_READ_*`. Only read-safe subcommands are allowed (`snapshots`, `check`, `stats`, `restore`, etc.). **`install-systemd` is not allowed** with READ-only credentials.

### Legacy flat keys

`RESTIC_REPOSITORY`, `RESTIC_PASSWORD`, and optional `RESTIC_S3_*` (no prefix). Treated as WRITE mode. Do not mix with `RESTIC_WRITE_*` or `RESTIC_READ_*`.

### Example (S3-compatible)

```yaml
restic_write_repository: s3:sgp1.digitaloceanspaces.com/my-bucket/myapp-prod-media
restic_write_password: strong-repo-password
restic_write_s3_access_key_id: REPLACE
restic_write_s3_secret_access_key: REPLACE
restic_write_s3_default_region: sgp1
```

Use a restic backend URL (`s3:…`), not a bare `https://…` URL. Use a **different** bucket prefix than pgBackRest `repo1-path` ([README_PGBACKREST.md](README_PGBACKREST.md)).

### Logging / verbosity

Restic uses repeated `-v` (0–4), not pgBackRest log-level names. Configure in **repo-root `tooling.yaml`** (`ops.restic`) and override per environment in **`docker/envs/<env>/info.yaml`** (not `credentials.yaml`):

| Location | Keys | Effect |
|----------|------|--------|
| `tooling.yaml` → `ops.restic` | `verbose`, `restore_verbose` | Project defaults |
| `info.yaml` → `restic:` | same | Per-environment override |
| `info.yaml` → `env:` | `restic_verbose`, `restic_restore_verbose` | Same (wins over `restic:`) |

`bkp_files restore` defaults to one `-v` when unset (like pgBackRest restore’s default `detail` console level). Set `restore_verbose: 0` under `ops.restic` or in `info.yaml` for a quiet restore. Extra `-v` on the CLI (`dk prod bkp_files -v restore`) adds to the configured level for that run.

## Auto-provision (DigitalOcean Spaces)

When WRITE-mode `restic_write_*` keys are missing, `dk <env> bkp_files …` can prompt to create repository credentials:

- Uses the same Spaces bucket defaults as pgBackRest (`digitalocean.spaces` in `tooling.yaml`)
- If `pgbr_s3_write_*` is already configured, reuses that bucket and access key; only restic keys are written
- Otherwise requires **`doctl`**, **`s3cmd`**, and **`sops`** (see [README_PGBACKREST.md](README_PGBACKREST.md))

## `bkp_files` subcommands

| Subcommand | Description |
|------------|-------------|
| `push [--source DIR] [--method rsync\|tar]` | Rsync host media into `django_media` (default source: `native.fetch_media.dest`; pair with `uv run native fetch media`). Not Restic. |
| `init` | `restic init` (new repository) |
| `backup` | Snapshot `django_media` |
| `snapshots` | List snapshots |
| `check` | Repository check |
| `stats` | Repository stats |
| `restore [SNAPSHOT]` | Restore into staging volume / media (destructive; confirms by env name) |
| `install-systemd [--dry-run] [--enable]` | Install timer on deploy host ([README_SYSTEMD.md](README_SYSTEMD.md)) |

`restore` refuses to run without a TTY unless you pass global `dk --yes`. Scheduled `backup` via systemd stays quiet unless `ops.restic.verbose` is set in `tooling.yaml`.

## New host checklist

Prerequisites: `docker_host` in `docker/envs/<env>/info.yaml`, WRITE or legacy restic credentials in `credentials.yaml`.

```bash
dk prod bkp_files init
dk prod bkp_files backup
dk prod bkp_files snapshots

dk prod bkp_files install-systemd --dry-run
dk prod bkp_files install-systemd --enable
```

`install-systemd` writes `@CONFIG_DIR@/restic-files-backup.env` (repository, password, S3 keys, `COMPOSE_PROJECT_NAME`, `RESTIC_FILES_DATA_VOLUME`) and copies `restic-files-backup.sh` to `ops.install_prefix`. Example env file: [`systemd/restic-files-backup.env.example`](src/catalpa_tooling/systemd/restic-files-backup.env.example).

## S3 and AWS env

Restic’s S3 backend reads `AWS_*` inside the container. Host env files may store only `RESTIC_S3_*`; install-systemd and the backup script map them to `AWS_ACCESS_KEY_ID`, etc.

For private-CA S3 endpoints, issue/install via [README_DC_BACKUP.md](README_DC_BACKUP.md). When `dc-backup-tls.yaml` exists, `DC_BACKUP_CA_FILE` is inferred (`{ops.config_dir}/tls/dc-backup-ca.crt`) and copied into the systemd env file; optional `DOCKER_ADD_HOST` still comes from `info.yaml` `env:`. The backup script and Zabbix `restic.snapshots` docker run mount the CA and set `AWS_CA_BUNDLE`.

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `RESTIC_WRITE_*, RESTIC_READ_*, and legacy … are mutually exclusive` | Mixed credential styles in one env. |
| `Skipping restic systemd files` | READ-only credentials or missing repository/password. |
| Wrong volume in snapshots | `COMPOSE_PROJECT_NAME` does not match the running stack. |
| `403` / lock errors on READ | Expected for read-only IAM; restore uses `--no-lock` where needed. |

Default image: `restic/restic:0.17.3` (override with `RESTIC_IMAGE` in the env file).
