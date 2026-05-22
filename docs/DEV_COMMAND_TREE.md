# `dev` command tree

Minimal tree for naming review. Run from the **application repo root** (where `tooling.yaml` lives) via `uv run dev …`.

Local server helpers without Docker. Remote fetch commands use `docker/envs/<name>/info.yaml` and optional project scripts under `paths.scripts` (default `tools/`).

```
dev
├── fetch
│   ├── db [-o PATH] [--env NAME]
│   └── media [--env NAME] [--host USER@HOST] [--dest DIR] [--partial]
│              [--legacy-path] [--remote PATH] [--compose-project NAME]
│
├── runserver [RUNSERVER_ARGS …]     # uv run ./manage.py runserver …
├── manage <django …>                # uv run ./manage.py …
├── reset-db [--from-dump PATH] [pg_restore args …]
├── pg-restore [--file PATH] [pg_restore args …]
├── vite                             # npm install + npm run dev (paths.frontend)
│
└── <name> [script args …]           # optional: scripts/dev-<name>.sh (per repo)
```

## `tooling.yaml` defaults

| Key | Role |
|-----|------|
| `paths.backend` | Django project dir (`uv run ./manage.py` cwd) |
| `paths.frontend` | Frontend dir for `vite` (`npm` / nvm when `.nvmrc` present) |
| `paths.env_local` | Loaded for `manage`, `runserver`, `reset-db`, `pg-restore` (e.g. `.env.local`) |
| `paths.fetch_db_dump` | Default output for `fetch db` |
| `paths.scripts` | Shell wrappers (`fetch_db.sh`, `dev-*.sh`) |
| `dev.fetch_media.dk_env` | Default `docker/envs/<name>/` for `fetch db` and `fetch media` (package default: `prod`) |
| `dev.fetch_media.dest` | Local media directory relative to repo root (default: `media`) |
| `dev.fetch_media.legacy` | Optional fixed host path for `--legacy-path` (`remote`, optional `ssh_host`) |
| `dev.reset_db.postgis` | If true, run `CREATE EXTENSION postgis` before migrate (default: `false`) |
| `dev.reset_db.pg_restore_args` | Extra `pg_restore` flags when restoring a dump (e.g. `--clean`, `--if-exists`) |
| `dev.reset_db.post_manage_commands` | `manage.py` argv lists after reset (local host, not compose exec) |
| `dev.reset_db.db_name_env` / `host_env` / … | Env var names in `paths.env_local` for libpq tools (first set wins) |
| `dev.reset_db.db_name_fallback` | Optional DB name override; else stem of `paths.fetch_db_dump` (`.custom`/`.dump`), else `{project.name}_db` |

**Host Postgres defaults** (when `.env.local` omits connection vars): `localhost:5432`, database from dump stem or `{project.name}_db`, **no** `PGUSER` / `DJANGO_DB_USER` (libpq and Django use the current OS user — typical for Postgres.app). Set `DJANGO_DB_USER` in `.env.local` when you need a dedicated role. The same defaults are applied to `uv run dev manage` / `runserver` so `reset-db` and Django use one database.

Example (catalpa-site — PostGIS + Wagtail hook only; DB name comes from `paths.fetch_db_dump`):

```yaml
dev:
  fetch_media:
    dk_env: prod
    dest: media
    legacy:
      remote: /backup/django_media
      ssh_host: site-production.catalpa.build
  reset_db:
    postgis: true
    post_manage_commands:
      - [sync_wagtail_sites, --profile, host]
```

## Top-level commands

| Command | Role |
|---------|------|
| `fetch db` | Run `scripts/fetch_db.sh`: `uv run dk <env> bkp_db pgdump` → `paths.fetch_db_dump` (remote `db` must be up) |
| `fetch media` | Rsync from deploy host (see below); implemented in catalpa-workspace-tooling (not a shell script) |
| `runserver` | `uv run ./manage.py runserver` with dev env defaults (`DJANGO_DEBUG=1`, `EMAIL_BACKEND_FOLDER`, `RQ_SYNCHRONOUS=1`) |
| `manage` | Any `manage.py` subcommand via `uv run` in `paths.backend` |
| `reset-db` | Local Postgres: see [reset-db](#reset-db) |
| `pg-restore` | `pg_restore` into app DB using the same env resolution as `reset-db` (stdin or `--file`) |
| `vite` | `npm install` then `npm run dev` in `paths.frontend` |

## `fetch db`

| Option | Default | Notes |
|--------|---------|--------|
| `-o`, `--output` | `paths.fetch_db_dump` | Custom-format dump path |
| `--env` | `dev.fetch_media.dk_env` | Passed to `fetch_db.sh` as `FETCH_DK_ENV` |

Requires `uv`, `bash`, and network/SSH access to the remote stack (same as `dk <env> bkp_db pgdump`).

## `fetch media`

**Docker volume mode (default):** SSH to `docker_host` from `docker/envs/<env>/info.yaml`, `docker volume inspect` on `{compose_project}_{ops.restic.data_volume}` (default `{project}_django_media`), then `rsync` the volume mount path.

| Option | Default | Notes |
|--------|---------|--------|
| `--env` | `dev.fetch_media.dk_env` | Which `info.yaml` supplies `docker_host` / `compose_project_name` |
| `--host` | from `info.yaml` | Override SSH target (`user@host` or bare hostname → `root@`) |
| `--dest` | `<repo>/dev.fetch_media.dest` | Local destination |
| `--partial` | off | Only `documents/` and `original_images/` |
| `--compose-project` | from `info.yaml` or `stack.compose_project_default` | Volume name prefix |
| `--legacy-path` | off | Use `dev.fetch_media.legacy` instead of Docker volume |
| `--remote` | `legacy.remote` | Remote directory when `--legacy-path` (override) |

Requires `rsync` and `ssh` on PATH.

**Legacy path mode:** rsync from a fixed directory on the SSH host (`--legacy-path`). Needs `legacy.remote` in `tooling.yaml` or `--remote`, and `legacy.ssh_host` or `--host`.

**Load into a local Compose stack** (named `django_media` volume, not host bind-mount):

```bash
uv run dk local bkp_files push
```

Default source is the same directory as `fetch media` (`dev.fetch_media.dest`, typically `./media/`). Uses rsync (incremental; `--delete` mirrors the host tree). On macOS Docker Desktop, rsync runs inside a one-off container. Fallback: `--method tar`. Confirm by typing the env name, or `dk --yes`.

## `reset-db`

Uses libpq client tools. Connection comes from `paths.env_local` and `dev.reset_db.*_env` keys (see table above).

**Source selection (default):**

1. If `paths.fetch_db_dump` exists and is non-empty → `pg_restore` that file (after `dropdb` / `createdb`).
2. Else → optional PostGIS (`dev.reset_db.postgis`) → `migrate`, or `scripts/dev-reset-db-post.sh` if present.

`--from-dump PATH` overrides the dump file; fails if the path is missing when explicitly set.

After a successful reset, runs `dev.reset_db.post_manage_commands` via local `uv run manage.py` (separate from `ops.post_db_restore`, which runs in Docker after compose restores).

| Option | Notes |
|--------|--------|
| `--from-dump PATH` | Force dump path (must exist) |
| trailing args | With `--from-dump`, forwarded to `pg_restore` |

Requires `dropdb`, `createdb`, and `pg_restore` (dump path) or `psql` (migrate path) on PATH.

## `pg-restore`

Uses the same DB env resolution as `reset-db`.

| Command | Notes |
|---------|--------|
| `pg-restore` | Adds `--no-owner` / `--no-acl` when missing. `--file PATH` or stdin |

## `runserver` / `manage`

Forwarded to `uv run ./manage.py …` in `paths.backend`. Unset `DJANGO_DEBUG` → `1`. Unset `EMAIL_BACKEND_FOLDER` → `paths.email_backend_dir`. When DB env vars from `dev.reset_db` are unset, sets the same host/port/name keys as `reset-db` (e.g. `DATABASE_HOST=localhost` for catalpa-site) so Django and libpq use one server. Clears inherited `VIRTUAL_ENV` before nested `uv run` to avoid workspace/backend env mismatch.

## `vite`

Runs in `paths.frontend`. If `.nvmrc` exists and `~/.nvm/nvm.sh` is present, uses `nvm use` before `npm`.

## Project extensions (`scripts/dev-*.sh`)

Files matching `scripts/dev-<name>.sh` register as `dev <name>` (kebab-case stem). They must not clash with built-ins: `fetch`, `runserver`, `manage`, `reset-db`, `pg-restore`, `vite`.

Arguments after the subcommand are passed to the script. Unknown flags on `pg-restore`, `reset-db --from-dump`, and extension commands are forwarded where supported.

## Related CLIs

| CLI | Overlap |
|-----|---------|
| `scripts` | Non-`dev-` shell helpers under `paths.scripts` (e.g. `fetch-db` → `fetch_db.sh`) |
| `dk` | Remote deploy, `bkp_db pgdump`, `bkp_files push`, `pull_media` (tar volume export; `dev fetch media` is rsync pull) |

Honcho / Procfile workflows in app repos are outside this CLI (e.g. `bash tools/dev_honcho.sh`).
