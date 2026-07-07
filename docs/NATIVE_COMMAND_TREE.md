# `native` command tree

Minimal tree for naming review. Run from the **application repo root** (where `tooling.yaml` lives) via `uv run native …`. The `dev` entry point is a deprecated alias.

Local server helpers without Docker. Remote fetch commands use `docker/envs/<name>/info.yaml` and optional project scripts under `paths.scripts` (default `tools/`).

```
native
├── fetch
│   ├── db [-o PATH] [--env NAME]
│   └── media [--env NAME] [--host USER@HOST] [--dest DIR] [--partial]
│              [--legacy-path] [--remote PATH] [--compose-project NAME]
│
├── runserver [RUNSERVER_ARGS …]     # uv run ./manage.py runserver …
├── manage <django …>                # uv run ./manage.py …
├── reset-db [--from-dump PATH] [pg_restore args …]
├── pg-restore [--file PATH] [pg_restore args …]
├── frontend                         # install + dev script (paths.frontend, native.frontend)
├── vite                             # alias for `frontend`
├── start                            # Honcho: runserver + frontend (see native.start)
│
└── <name> [script args …]           # optional: scripts/native-<name>.sh (per repo)
```

## `tooling.yaml` defaults

| Key | Role |
|-----|------|
| `paths.backend` | Django project dir (`uv run ./manage.py` cwd) |
| `paths.frontend` | Frontend dir for `frontend` / `vite` (npm, yarn, or pnpm; nvm when `.nvmrc` present) |
| `paths.env_local` | Loaded for `manage`, `runserver`, `reset-db`, `pg-restore` (e.g. `.env.local`) |
| `paths.email_backend_dir` | Default `EMAIL_BACKEND_FOLDER` for host `manage` / `runserver` when unset |
| `paths.media_dir` | Optional host media tree for `native runserver` / `manage` (`DJANGO_MEDIA_ROOT` when unset) |
| `native.fetch.databases` | Per-DB fetch sources (`app` required when set): `db_name`, `via` (`ssh_native` \| `ssh_docker` \| `dk`), optional `ssh_host`, `container`, `pg_user`, `dk_env`, `dump` |
| `native.fetch.dk_env` | Default source env for `via: dk` and `dk fetch` (falls back to `native.fetch_media.dk_env`) |
| `native.fetch.ssh_host` | Default SSH target for `via: ssh_*` methods |
| `paths.fetch_db_dump` | Default output for `databases.app` |
| `paths.fetch_metabase_db_dump` | Default output for `databases.metabase` |
| `paths.scripts` | Shell wrappers (`native-*.sh`; legacy `fetch_db.sh` when `native.fetch.databases` omitted) |
| `native.fetch_media.dk_env` | Legacy default env when `native.fetch` omitted (package default: `prod`) |
| `native.fetch_media.dest` | Local media directory relative to repo root (default: `media`) |
| `native.fetch_media.legacy` | Optional fixed host path for `--legacy-path` (`remote`, optional `ssh_host`, optional `default: true`) |
| `native.reset_db.postgis` | If true, run `CREATE EXTENSION postgis` before migrate on host reset (default: `false`); for compose `pgrestore` / `dk transfer`, pre-creates PostGIS, grants catalog tables to the app user, and defaults compose `pg_restore` to `--role postgres` (unless `restore_as_super` or `pg_restore_args` sets another role) |
| `native.reset_db.restore_as_super` | If true (default: `false`), compose restore temporarily promotes ``APP_USER`` to superuser and reloads with ``--role APP_USER`` instead of the ``postgis`` default ``--role postgres`` — for dbsamizdat-friendly ownership while extension DDL/comments still succeed |
| `native.reset_db.pg_restore_args` | Extra `pg_restore` flags for dump restore (`native reset-db`, `native pg-restore`, **`dk <env> db restore`**, **`dk <env> db pgrestore`**, **`dk transfer`**) — e.g. `--clean`, `--if-exists`, `--role=postgres` |
| `native.reset_db.post_manage_commands` | `manage.py` argv lists after reset (local host, not compose exec) |
| `native.reset_db.db_name_env` / `host_env` / … | Env var names in `paths.env_local` for libpq tools (first set wins) |
| `native.reset_db.db_name_fallback` | Optional DB name override; else stem of `paths.fetch_db_dump` (`.custom`/`.dump`), else `{project.name}_db` |
| `native.frontend.package_manager` | Optional `npm`, `yarn`, or `pnpm` (auto-detect from `package.json` / lockfiles when omitted) |
| `native.frontend.script` | `package.json` script to run (default: `dev`) |
| `native.frontend.install` | Run package manager install before dev script (default: `true`) |
| `native.frontend.node_version` | Optional Node version for nvm (e.g. `22`); `.nvmrc` in `paths.frontend` takes precedence |
| `native.frontend.env` | Extra env vars for the dev-server subprocess only |
| `native.start.procfile` | Optional checked-in Procfile path (relative to repo root); omit for auto-generated Django + frontend default |
| `native.django.port` | Host port for `native runserver` (e.g. `8005` for PEP digit 5); default Django `8000` when unset |
| `native.start.ports` | TCP ports freed on exit when listeners remain (default: `[8000, 8080]`) |
| `native.start.migrate` | When using auto-generated Procfile, run `native manage migrate` before `runserver` (default: `true`) |

**Host Postgres defaults** (when `.env.local` omits connection vars): `localhost:5432`, database from dump stem or `{project.name}_db`. **`reset-db` / `pg-restore` libpq tools always omit `-U` / `PGUSER`** (current OS user, typical Postgres.app trust auth) even when `DJANGO_DB_USER` or `POSTGRES_USER` is set for Docker or Django. Django `manage` / `runserver` use the same host/port/name defaults when those vars are unset; set `DJANGO_DB_USER` in `.env.local` only when Django itself needs a dedicated role.

Example (catalpa-site — PostGIS + Wagtail hook only; DB name comes from `paths.fetch_db_dump`):

```yaml
native:
  fetch_media:
    dk_env: prod
    dest: media
    legacy:
      default: true
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
| `fetch db` | Deprecated wrapper — use `dk fetch db` (config-driven via `native.fetch.databases`; legacy: `scripts/fetch_db.sh`) |
| `fetch media` | Rsync from deploy host (see below); also available as `dk fetch media` |
| `runserver` | `uv run ./manage.py runserver` with dev env defaults (`DJANGO_DEBUG=1`, `EMAIL_BACKEND_FOLDER`, `RQ_SYNCHRONOUS=1`) |
| `manage` | Any `manage.py` subcommand via `uv run ./manage.py` in `paths.backend` |
| `reset-db` | Local Postgres: see [reset-db](#reset-db) |
| `pg-restore` | `pg_restore` into app DB using the same env resolution as `reset-db` (stdin or `--file`) |
| `frontend` | Install deps and run `native.frontend.script` in `paths.frontend` |
| `vite` | Alias for `frontend` |
| `start` | Honcho supervisor: `native runserver` + `native frontend` (Postgres must already be running) |

## `fetch db`

| Option | Default | Notes |
|--------|---------|--------|
| `-o`, `--output` | `paths.fetch_db_dump` | Custom-format dump path |
| `--env` | `native.fetch_media.dk_env` | Passed to `fetch_db.sh` as `FETCH_DK_ENV` |

Requires `uv`, `bash`, and network/SSH access to the remote stack (same as `dk <env> bkp_db pgdump`).

## `fetch media`

**Docker volume mode (default):** SSH to `docker_host` from `docker/envs/<env>/info.yaml`, `docker volume inspect` on `{compose_project}_{ops.restic.data_volume}` (default `{project}_django_media`), then `rsync` the volume mount path.

| Option | Default | Notes |
|--------|---------|--------|
| `--env` | `native.fetch_media.dk_env` | Which `info.yaml` supplies `docker_host` / `compose_project_name` |
| `--host` | from `info.yaml` | Override SSH target (`user@host` or bare hostname → `root@`) |
| `--dest` | `<repo>/native.fetch_media.dest` | Local destination |
| `--partial` | off | Only `documents/` and `original_images/` |
| `--compose-project` | from `info.yaml` or `stack.compose_project_default` | Volume name prefix |
| `--legacy-path` / `--no-legacy-path` | `native.fetch_media.legacy.default` (else off) | Use `native.fetch_media.legacy` instead of Docker volume |
| `--remote` | `legacy.remote` | Remote directory when `--legacy-path` (override) |

Requires `rsync` and `ssh` on PATH.

**Legacy path mode:** rsync from a fixed directory on the SSH host (`--legacy-path`, or by default when `legacy.default: true` in tooling.yaml). Needs `legacy.remote` in tooling.yaml or `--remote`, and `legacy.ssh_host` or `--host`. Use `--no-legacy-path` to force Docker volume mode.

**Load into a local Compose stack** (named `django_media` volume, not host bind-mount):

```bash
uv run dk local bkp_files push
```

Default source is the same directory as `fetch media` (`native.fetch_media.dest`, typically `./media/`). Uses rsync (incremental; `--delete` mirrors the host tree). On macOS Docker Desktop, rsync runs inside a one-off container. Fallback: `--method tar`. Confirm by typing the env name, or `dk --yes`.

## `reset-db`

Uses libpq client tools. Connection comes from `paths.env_local` and `native.reset_db.*_env` keys (see table above).

**Source selection (default):**

1. If `paths.fetch_db_dump` exists and is non-empty → `pg_restore` that file (after `dropdb` / `createdb`).
2. Else → optional PostGIS (`native.reset_db.postgis`) → `migrate`, or `scripts/native-reset-db-post.sh` if present.

`--from-dump PATH` overrides the dump file; fails if the path is missing when explicitly set.

After a successful reset, runs `native.reset_db.post_manage_commands` via local `uv run ./manage.py` (separate from `ops.post_db_restore`, which runs in Docker after compose restores).

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

Forwarded to `uv run ./manage.py …` in `paths.backend`. Unset `DJANGO_DEBUG` → `1`. Unset `EMAIL_BACKEND_FOLDER` → `paths.email_backend_dir`. Unset `DJANGO_MEDIA_ROOT` → `paths.media_dir` when configured (same tree as `native fetch media` / `dk dev` host mount). When DB env vars from `native.reset_db` are unset, sets the same host/port/name keys as `reset-db` (e.g. `DATABASE_HOST=localhost` for catalpa-site) so Django and libpq use one server. Clears inherited `VIRTUAL_ENV` before nested `uv run` to avoid workspace/backend env mismatch.

## `frontend` / `vite`

Runs in `paths.frontend`. Package manager: `native.frontend.package_manager`, or auto-detect from `package.json` `packageManager`, then yarn/pnpm lockfiles, else `npm`. Dev script: `native.frontend.script` (default `dev`). Skips install when `native.frontend.install: false`. Applies `native.frontend.env` to the dev-server process only.

Node: when `~/.nvm/nvm.sh` exists, uses `nvm use` if `.nvmrc` is present in `paths.frontend`, else `nvm use <version>` when `native.frontend.node_version` is set.

Example (webpack frontend):

```yaml
native:
  frontend:
    package_manager: yarn
    script: start
    node_version: "22"
    env:
      WEBPACK_DEVSERVER_PROXY_TARGET: http://127.0.0.1:8000
      SKIP_SW: "true"
  start:
    ports: [8000, 8080]
```

## `start`

Runs Django and the frontend dev server together via [Honcho](https://github.com/nickstenning/honcho) (dependency of `catalpa-workspace-tooling`). Postgres must already be reachable (host Postgres or `uv run dk dev up -d db`).

**Procfile resolution:**

1. `native.start.procfile` when set (project-specific commands, ports, env exports).
2. Otherwise auto-generated:

```
web: sh -c 'uv run native manage migrate && uv run native runserver'
frontend: uv run native frontend
```

When `native.start.migrate: false`, the `web` line omits migrate.

On exit (Ctrl-C or process end), configured `native.start.ports` are freed if still in use (`lsof` + SIGTERM/SIGKILL).

Example (auto-generated Procfile — Django + frontend):

```yaml
native:
  frontend:
    package_manager: yarn
    script: start
    node_version: "22"
    env:
      WEBPACK_DEVSERVER_PROXY_TARGET: http://127.0.0.1:8000
      SKIP_SW: "true"
  start:
    ports: [8000, 8080]
```

Projects with custom webpack or Django ports (e.g. catalpa-site) can check in a Procfile and set `native.start.procfile: tools/Procfile`.

## Project extensions (`scripts/native-*.sh`)

Files matching `scripts/native-<name>.sh` register as `native <name>` (kebab-case stem). They must not clash with built-ins: `fetch`, `runserver`, `manage`, `reset-db`, `pg-restore`, `frontend`, `vite`, `start`.

Arguments after the subcommand are passed to the script. Unknown flags on `pg-restore`, `reset-db --from-dump`, and extension commands are forwarded where supported.

## Related CLIs

| CLI | Overlap |
|-----|---------|
| `scripts` | Non-`native-` shell helpers under `paths.scripts` (string or list; e.g. `fetch-db` → `fetch_db.sh`) |
| `dk` | Remote deploy, `db pgdump`, `files push`, `pull_media` (tar volume export; `native fetch media` is rsync pull) |
