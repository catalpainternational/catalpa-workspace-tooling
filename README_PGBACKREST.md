# pgBackRest (S3, `bkp_db`, WAL archive)

Database backups and WAL archiving via [pgBackRest](https://pgbackrest.org) with an S3-compatible repository. Credentials and deploy env live in the **consumer app repo** under `docker/envs/<env>/`; this package provides `dk <env> bkp_db` and systemd timer install.

**Related docs**

- [README_SYSTEMD.md](README_SYSTEMD.md) — install `*-pgbackrest-backup-*.timer` on the deploy host
- [ZABBIX_README.md](ZABBIX_README.md) — `pgbackrest.info` UserParameter for monitoring
- [README.md](README.md) — main tooling overview

## Credentials per environment

Edit `docker/envs/<env>/credentials.yaml` (SOPS-encrypted in production). Use **lowercase** keys; `dk` uppercases them when loading env ([`env_yaml.py`](src/catalpa_tooling/env_yaml.py)).

### WRITE mode (backup hosts)

| `credentials.yaml` key | Process env | Purpose |
|------------------------|-------------|---------|
| `pgbr_s3_write_bucket` | `PGBR_S3_WRITE_BUCKET` | S3 bucket |
| `pgbr_s3_write_region` | `PGBR_S3_WRITE_REGION` | Region |
| `pgbr_s3_write_key` | `PGBR_S3_WRITE_KEY` | Access key |
| `pgbr_s3_write_secret` | `PGBR_S3_WRITE_SECRET` | Secret key |
| `pgbr_s3_write_repo_path` | `PGBR_S3_WRITE_REPO_PATH` | Prefix in bucket (`repo1-path`) |
| `pgbr_s3_write_stanza` | `PGBR_S3_WRITE_STANZA` | Stanza name (e.g. `main`) |
| `pgbr_s3_write_endpoint` | `PGBR_S3_WRITE_ENDPOINT` | Optional (Spaces, MinIO, etc.) |

All keys except `endpoint` must be non-empty for WRITE mode. A partial set is treated as “no S3” (local baseline only).

Optional: `pgbr_s3_write_retention_full` → `PGBR_S3_WRITE_RETENTION_FULL` (numeric retention override).

For systemd timers, set `pgbr_db_container` → `PGBR_DB_CONTAINER` if auto-discovery cannot find exactly one Postgres container on the host.

Example fragment:

```yaml
pgbr_s3_write_bucket: my-backups
pgbr_s3_write_region: sgp1
pgbr_s3_write_endpoint: sgp1.digitaloceanspaces.com
pgbr_s3_write_key: REPLACE
pgbr_s3_write_secret: REPLACE
pgbr_s3_write_repo_path: /myapp/prod/pgbackrest
pgbr_s3_write_stanza: main
# pgbr_db_container: myapp_db_1
```

Decrypt locally with your SOPS age key, or:

```bash
sops exec-env docker/envs/prod/credentials.yaml -- dk prod …
```

### READ mode (restore-only hosts)

Use `pgbr_s3_read_*` → `PGBR_S3_READ_*` with the same suffixes. **Do not set WRITE and READ in the same environment** — `dk` rejects both prefixes.

`stanza-create` and scheduled systemd backups require WRITE mode only.

## Auto-provision (DigitalOcean Spaces)

When WRITE-mode `pgbr_s3_write_*` keys are missing, `dk <env> bkp_db` commands that need S3 (backup, `configure verify`, `install-systemd`, etc.) can create a Spaces bucket and access key interactively:

- Host **`doctl`** — Spaces access keys (`doctl spaces keys create`)
- Host **`s3cmd`** — bucket create / existence check (`s3cmd mb`, `s3cmd info`)
- Host **`sops`** — updates `docker/envs/<env>/credentials.yaml` via `sops set`

Defaults come from `tooling.yaml` (`digitalocean.region`, optional `digitalocean.spaces.*`). Example:

```yaml
digitalocean:
  region: sgp1
  spaces:
    bucket: marktwain
    pgbackrest_repo_path: /marktwain/prod/pgbackrest
```

Override binaries with `DOCTL_BIN` / `S3CMD_BIN`. Use global `--yes` to accept provisioning without typing `y`. `bkp_db pgdump` / bare `configure` do not trigger provisioning.

## `tooling.yaml` (`ops.pgbackrest`)

```yaml
ops:
  pgbackrest:
    postgres_conf: 30-myapp-pgbackrest-archive.conf   # drop-in on postgres_conf volume
    pgbackrest_conf: 50-myapp-managed.conf            # drop-in on pgbackrest_conf volume
    default_registry: ghcr.io/org/myapp-postgres
    restore_temp_prefix: myapp_pgrestore_
```

Filenames must match what your Postgres image and compose volumes expect.

## Apply configuration to a new host

Prerequisites: `docker/envs/<env>/info.yaml` has `docker_host` (SSH URL to the deploy machine).

From the **application repo root**:

```bash
# 1) Create external compose volumes on the remote Docker host
dk prod ensure_volumes

# 2) Write pgBackRest + Postgres archive config into named volumes
dk prod bkp_db configure
dk prod bkp_db configure verify          # optional: version + online check (db must be up)
dk prod bkp_db configure stanza-create   # first time only; WRITE mode + initialized PGDATA

# 3) Start the stack (also materializes on plain `up`)
dk prod up -d
# or: dk prod up --provision -d

# 4) Sanity-check
dk prod bkp_db info
dk prod bkp_db backup full

# 5) Scheduled backups on the host
dk prod bkp_db install-systemd --dry-run
dk prod bkp_db install-systemd --enable
```

| Step | Effect |
|------|--------|
| `ensure_volumes` | Creates compose `external` volumes (`*_postgres_data`, `*_postgres_conf`, `*_pgbackrest_conf`, …) on `DOCKER_HOST`. |
| `bkp_db configure` | Renders managed pgBackRest INI and WAL `archive_command` into conf volumes via one-off `docker run`. |
| `configure verify` | `pgbackrest version` on the conf volume, then online `check` in the running `db` container. |
| `configure stanza-create` | Registers stanza metadata in S3 (once per new repo path; needs initialized PostgreSQL data). |
| `install-systemd` | See [README_SYSTEMD.md](README_SYSTEMD.md). |

Without any `PGBR_S3_WRITE_*`, materialize leaves **WAL archiving off** and a local-repo baseline (suitable for dev).

## `bkp_db` subcommands

| Subcommand | Description |
|------------|-------------|
| `configure` | Materialize volume config from `PGBR_S3_*` |
| `configure verify` | Preflight + online check (db running) |
| `configure stanza-create` | `pgbackrest stanza-create` (WRITE only) |
| `install-systemd [--dry-run] [--enable]` | Install timer units on deploy host |
| `info` | `pgbackrest info` in db container |
| `check` | Online `pgbackrest check` |
| `version` | `pgbackrest version` in db container |
| `backup full\|incr\|diff` | Online backup (db running) |
| `pgdump [args…]` | `pg_dump` via compose |
| `pgrestore [--file ARCHIVE] [args…]` | Restore from custom-format dump |
| `restore [pgBackRest args…]` | Offline pgBackRest restore |

Offline restore may require global `dk --yes` when not attached to a TTY.

## Optional tuning (process env)

Not part of `pgbr_s3_write_*`; set in credentials or `info.yaml` env (uppercase):

- `PGBR_REPO1_BUNDLE`, `PGBR_REPO1_BLOCK`, `PGBR_ARCHIVE_ASYNC` — `y`/`n`
- `PGBR_PROCESS_MAX`, `PGBR_COMPRESS_LEVEL`
- `PGBR_REPO1_RETENTION_FULL_TYPE`, `PGBR_REPO1_RETENTION_FULL`

See [`pgbackrest_volume_config.py`](src/catalpa_tooling/pgbackrest_volume_config.py).

## Systemd timers

`install-systemd` can install full, incremental, and optional **differential** timer units ([README_SYSTEMD.md](README_SYSTEMD.md)). The backup script and `bkp_db backup diff` already support `--type=diff`; differential timers are opt-in via `ops.systemd_units.pgbackrest` and `timers_enable_pgbackrest`.

For most small deployments, **weekly full + daily incr** is enough. Use the diff timer instead of (not alongside) daily incr when you want pgBackRest’s documented **full weekly + diff Mon–Sat** pattern or a custom hybrid schedule — see [README_SYSTEMD.md](README_SYSTEMD.md#pgbackrest-backup-schedules).

## Systemd env file

After `install-systemd`, the host has `@CONFIG_DIR@/pgbackrest-backup.env` with `PGBR_DB_CONTAINER` and `PGBR_STANZA`. Example: [`systemd/pgbackrest-backup.env.example`](src/catalpa_tooling/systemd/pgbackrest-backup.env.example).

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `missing required env for write mode` | Incomplete `pgbr_s3_write_*` in credentials. |
| `PGBR_S3_WRITE_* and PGBR_S3_READ_* are mutually exclusive` | Both prefixes set in one env. |
| `Skipping pgBackRest systemd files: PGBR_S3_WRITE_STANZA is not set` | WRITE credentials missing. |
| `Could not discover a unique db container` | Stack not up, multiple Postgres containers, or set `pgbr_db_container`. |
| Archive still off after deploy | Run `bkp_db configure` or `up --provision`; entrypoint may not overwrite provisioned drop-ins. |
| `stanza-create` fails on empty PGDATA | Start `db` once so initdb creates `global/pg_control`, then retry. |

App-specific Postgres image and compose notes belong in the consumer repo (e.g. `docker/postgres/README.md`).
