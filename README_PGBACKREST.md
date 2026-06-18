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

When WRITE-mode `pgbr_s3_write_*` keys are missing, `dk <env> bkp_db` commands that require write access (backup, `init`, `configure stanza-create`, `install-systemd`, etc.) can create a Spaces bucket and access key interactively. **`restore` and `configure verify` use READ or WRITE credentials and never auto-provision** — set `pgbr_s3_read_*` on restore-only hosts (e.g. local/dev).

- Host **`doctl`** — Spaces access keys (`doctl spaces keys create`); PAT scopes in [README.md](README.md#digitalocean-pat-scopes)
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
    restore_temp_prefix: myapp_pgrestore_   # optional; default {project.name}_pgrestore_
    data_volume: pgdata                               # compose volumes: key (default postgres_data)
    pg1_path: /var/lib/postgresql/18/docker           # PG 18+ image PGDATA (see entrypoint)
    # log_level_console: info
    # log_level_stderr: warn
    # restore_log_level_console: info
```

Filenames must match what your Postgres image and compose volumes expect. When the `db` service mounts `pgdata:/var/lib/postgresql`, set `pg1_path` to the real cluster directory (official Postgres 18+ images use `/var/lib/postgresql/<major>/docker`, not `…/data`). Override per deploy with env `PGBR_PG1_PATH` if needed. After changing `pg1_path`, run `bkp_db configure` again so the `pgbackrest_conf` volume picks up the new `pg1-path`.

## New backup host checklist

Prerequisites: `docker/envs/<env>/info.yaml` has `docker_host` (SSH URL to the deploy machine) and complete `pgbr_s3_write_*` in credentials (or use auto-provision on write commands).

From the **application repo root**:

```bash
dk prod host create                    # droplet + docker_host (if not already set)

dk prod bkp_db init                    # volumes + conf + db init + stanza (idempotent)

dk prod up -d                          # full stack (volumes + conf already done by init or any prior up)
dk prod bkp_db install-systemd --enable
dk prod bkp_db backup full             # optional sanity check
```

| Step | What happens |
|------|----------------|
| `host create` | Deploy machine and `docker_host` in `info.yaml`. |
| `bkp_db init` | Creates external volumes, materializes pgBackRest/Postgres conf, starts `db` if needed, registers S3 stanza (skips if already present). |
| `up -d` | Starts the rest of the stack (also runs volume ensure + materialize if you skipped `init`). |
| `install-systemd` | Host timers for full/incr backups — [README_SYSTEMD.md](README_SYSTEMD.md). |

**Equivalent manual flow** (no `init`):

```bash
dk prod host create
dk prod bkp_db configure stanza-create   # materializes conf if needed, starts db, creates stanza
dk prod up -d
dk prod bkp_db install-systemd --enable
```

### What `dk <env> up` does automatically

Every **`dk <env> up …`** (including `up -d db`) runs **`ensure_volumes`** and **`materialize_configs`** before calling `docker compose`. You do **not** need separate `ensure_volumes` or `bkp_db configure` before `up` unless you want config on disk without starting containers.

The dk-only flag **`--provision`** on `up` is stripped before compose and does not add extra steps — plain `up` already materializes.

### Advanced (without starting the stack)

| Command | Effect |
|---------|--------|
| `ensure_volumes` | Create compose `external` volumes only. |
| `bkp_db configure` | Materialize pgBackRest INI + WAL `archive_command` into conf volumes only. |
| `bkp_db configure verify` | `pgbackrest version` on conf volume, then online `check` (`db` must be running). |
| `bkp_db configure stanza-create` | Same stanza step as `init` (starts `db` if PGDATA empty; idempotent). |

Without any `PGBR_S3_WRITE_*`, materialize leaves **WAL archiving off** and a local-repo baseline (suitable for dev).

### Console / stderr log levels

pgBackRest levels: `off`, `error`, `warn`, `info`, `detail`, `debug`, `trace`. Set in **`tooling.yaml`** (`ops.pgbackrest`) and override per host in **`docker/envs/<env>/info.yaml`** (not `credentials.yaml`):

| Location | Keys |
|----------|------|
| `tooling.yaml` → `ops.pgbackrest` | `log_level_console`, `log_level_stderr`, `restore_log_level_console` |
| `info.yaml` → `pgbackrest:` | same |
| `info.yaml` → `env:` | `pgbr_log_level_console`, `pgbr_log_level_stderr`, `pgbr_restore_log_level_console` |

All `bkp_db` pgBackRest invocations use `log_level_console` / `log_level_stderr` when set. Offline **`restore`** uses `restore_log_level_console`, then `log_level_console`, then defaults to **`detail`** for console progress. Systemd backup units pick up the same vars when installed via `bkp_db install-systemd` (from the merged deploy env).

## `bkp_db` subcommands

| Subcommand | Description |
|------------|-------------|
| `init` | Greenfield backup host: volumes + materialize + start `db` + stanza-create (idempotent) |
| `configure` | Materialize volume config from `PGBR_S3_*` |
| `configure verify` | Preflight + online check (db running) |
| `configure stanza-create` | Stanza in S3 (WRITE only; starts `db` if needed; skips if stanza exists) |
| `install-systemd [--dry-run] [--enable]` | Install timer units on deploy host |
| `info` | `pgbackrest info` in db container |
| `check` | Online `pgbackrest check` |
| `version` | `pgbackrest version` in db container |
| `backup full\|incr\|diff` | Online backup (db running) |
| `pgdump [args…]` | `pg_dump` via compose |
| `pgrestore [--file ARCHIVE] [args…]` | Restore from custom-format dump (`paths.fetch_db_dump` when stdin is a TTY and `--file` omitted; starts `db` if needed; drop/recreate app DB first; PostGIS only when `native.reset_db.postgis` is true in `tooling.yaml`) |
| `restore [pgBackRest args…]` | Offline pgBackRest restore |

On a new host, if the `pgbackrest_conf` volume has no managed config yet, **`restore` prompts to run `bkp_db configure`** (same as materialize) before the destructive restore confirmation. With global **`dk --yes`**, configure runs automatically without the y/n prompt.

Offline restore may require global `dk --yes` when not attached to a TTY.

After a successful **`restore`** or **`pgrestore`**, the tooling may run project-configured Django management commands (see below). If a hook fails, the database is already restored; re-run with `dk <env> manage …`.

## Post-restore Django commands (`ops.post_db_restore`)

Optional hooks in repo-root `tooling.yaml` (default: none). Applied after successful Compose DB restores (`bkp_db restore`, `bkp_db pgrestore`, `dk transfer` DB leg).

```yaml
ops:
  post_db_restore:
    envs: [local, dev, staging]   # optional; omit to run on every env
    manage_commands:
      - [sync_wagtail_sites, --profile, "{env_name}"]
      - migrate --noinput          # string form is split with shlex
```

Each entry is arguments after `manage.py` (same as `dk <env> manage …`). `{env_name}` is replaced with the deploy env name. The web service is started and health-checked before commands run.

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
| Archive still off after deploy | Run `bkp_db configure` or any `dk <env> up`; entrypoint may not overwrite provisioned drop-ins. |
| `stanza-create` fails on empty PGDATA | Use `bkp_db init` or `configure stanza-create` (starts `db` automatically); or `dk <env> up -d db` then retry. |
| `info` shows `missing stanza path` after `init` skipped stanza-create | Broken or empty S3 repo prefix; clear repo path or fix credentials, then re-run `bkp_db configure stanza-create` (tooling only skips when `info` reports `status: ok`). |
| `[037]: restore command requires option: pg1-path` | Run `bkp_db configure` on the host, or accept the prompt when `restore` detects an empty `pgbackrest_conf` volume. |
| `invalid checkpoint record` / `could not locate required checkpoint record` after restore | Often an **online** deploy backup with no WAL archive chain in the repo (`pgbackrest info` shows `wal archive min/max: none present`). Wipe `postgres_data`, restore from an **offline** full backup, or pass `dk <env> db restore -- --type=immediate --archive-mode=off`. Re-create deploy backups with Postgres stopped (see `upgrade_postgres` `backup.sh`). |
| `dk <env> db restore -- …` passes invalid option `--` to pgBackRest | Omit the `--` separator, or upgrade tooling (leading `--` is stripped before invoke). |

App-specific Postgres image and compose notes belong in the consumer repo (e.g. `docker/postgres/README.md`).
