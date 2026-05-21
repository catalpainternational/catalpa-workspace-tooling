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

Example (catalpa-site):

```yaml
dev:
  fetch_media:
    dk_env: prod
    dest: media
    legacy:
      remote: /backup/django_media
      ssh_host: site-production.catalpa.build
```

## Top-level commands

| Command | Role |
|---------|------|
| `fetch db` | Run `scripts/fetch_db.sh`: `uv run dk <env> bkp_db pgdump` → `paths.fetch_db_dump` (remote `db` must be up) |
| `fetch media` | Rsync from deploy host (see below); implemented in catalpa-workspace-tooling (not a shell script) |
| `runserver` | `uv run ./manage.py runserver` with dev env defaults (`DJANGO_DEBUG=1`, `EMAIL_BACKEND_FOLDER`, `RQ_SYNCHRONOUS=1`) |
| `manage` | Any `manage.py` subcommand via `uv run` in `paths.backend` |
| `reset-db` | Local Postgres: `dropdb` → `createdb` → PostGIS → `migrate`, or `scripts/dev-reset-db-post.sh` if present |
| `pg-restore` | `pg_restore` into app DB using `POSTGRES_*` from `.env.local` (stdin or `--file`) |
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

## `reset-db` / `pg-restore`

Uses libpq client tools and `POSTGRES_DB` / `POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_USER` / `POSTGRES_PASSWORD` from `paths.env_local` (default database name `django`).

| Command | Notes |
|---------|--------|
| `reset-db` | Without `--from-dump`: PostGIS + migrate (or project hook). With `--from-dump`: `pg_restore` after recreate; extra args forwarded |
| `pg-restore` | Adds `--no-owner` / `--no-acl` when missing. `--file PATH` or stdin |

## `runserver` / `manage`

Forwarded to `uv run ./manage.py …` in `paths.backend`. Unset `DJANGO_DEBUG` → `1`. Unset `EMAIL_BACKEND_FOLDER` → `paths.email_backend_dir`. Clears inherited `VIRTUAL_ENV` before nested `uv run` to avoid workspace/backend env mismatch.

## `vite`

Runs in `paths.frontend`. If `.nvmrc` exists and `~/.nvm/nvm.sh` is present, uses `nvm use` before `npm`.

## Project extensions (`scripts/dev-*.sh`)

Files matching `scripts/dev-<name>.sh` register as `dev <name>` (kebab-case stem). They must not clash with built-ins: `fetch`, `runserver`, `manage`, `reset-db`, `pg-restore`, `vite`.

Arguments after the subcommand are passed to the script. Unknown flags on `pg-restore`, `reset-db --from-dump`, and extension commands are forwarded where supported.

## Related CLIs

| CLI | Overlap |
|-----|---------|
| `scripts` | Non-`dev-` shell helpers under `paths.scripts` (e.g. `fetch-db` → `fetch_db.sh`) |
| `dk` | Remote deploy, `bkp_db pgdump`, `pull_media` (tar via Docker; different from `dev fetch media` rsync) |

Honcho / Procfile workflows in app repos are outside this CLI (e.g. `bash tools/dev_honcho.sh`).
