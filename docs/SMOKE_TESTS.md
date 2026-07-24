# Smoke / CI / functional tests (`tests ci`, `tests functional`)

Project-health and Playwright checks for Django + Docker Compose stacks. Tooling orchestrates the stack; your repo supplies tests under `{paths.frontend}/smoke/`.

**Requires:** catalpa-workspace-tooling with `tests ci` / `tests functional` (formerly `tests smoke`).

## Overview

| Command | Role |
|---------|------|
| `uv run tests ci` | CI gate: empty migrate, makemigrations, frontend type-check/build, guest Playwright |
| `uv run tests functional` | Skip gate; run functional Playwright (`-m elearning` by default) |
| `uv run tests functional headed` | Same, visible browser with default slow-mo (250 ms) |
| `uv run tests smoke` | **Deprecated** alias for `tests ci` (or `tests smoke --functional` → functional) |

## What tooling runs

### CI gate (`tests ci`)

Ordered pipeline (implemented in `smoke_cli.run_smoke`):

1. Resolve `docker/envs/<env>/info.yaml` and compose file
2. Optional `docker compose up -d` (skip with `--no-up`); same preflight as `dk <env> up`
3. Wait for Postgres (`pg_isready`)
4. **Empty migrate:** ephemeral `{dbname}_smoke_empty` locally (default; skip with `--no-fresh-db`). With `--ci` / `CI=1`, primary DB is assumed empty.
5. Primary DB: `migrate` (`--check-only` → `migrate --check`)
6. `manage check`
7. `makemigrations --check --dry-run`
8. **Frontend type-check + production build** — prefer `docker compose run --no-deps node pnpm run …` when a `node` service exists (image `node_modules`); otherwise host package manager under `paths.frontend`
9. Wait for `stack.healthcheck` on the web service
10. Poll HTTP GET `{site_origin}/` until 2xx/3xx
11. Guest pytest under `{paths.frontend}/smoke` (elearning skipped unless you pass a marker)

### Functional (`tests functional` / `tests functional headed`)

Skips the migrate / makemigrations / frontend-build gate. Waits for HTTP, then runs pytest with `-m elearning` by default. `headed` adds `--headed --slowmo=250` (override with your own `--slowmo=`).

```mermaid
flowchart TD
  start[tests ci / functional] --> mode{command}
  mode -->|ci| up{Stack running?}
  up -->|no| composeUp[compose up -d]
  up -->|yes| emptyMigrate
  composeUp --> emptyMigrate[empty DB migrate]
  emptyMigrate --> migrate[primary migrate + check + makemigrations]
  migrate --> build[type-check + build]
  build --> web[healthcheck + HTTP]
  web --> pytest[guest pytest]
  mode -->|functional| web2[healthcheck + HTTP]
  web2 --> pytestF[elearning pytest]
```

## Port allocation

Dev tests use `site_origin` from `docker/envs/dev/info.yaml` (port **901N**). See **`bero/docs/PORTS.md`**.

## Project prerequisites

### Required

| Requirement | Where | Notes |
|-------------|-------|-------|
| Valid `tooling.yaml` | repo root | Same manifest as `dk` / other `tests` subcommands |
| `stack.services.web` + `stack.services.db` | `tooling.yaml` | Web container must expose `./manage.py` |
| `stack.healthcheck` | `tooling.yaml` | URL when app is healthy (bero: `/cms/`) |
| `site_origin` | `docker/envs/dev/info.yaml` | HTTP probe + `SMOKE_FE_URL` |
| `{paths.frontend}/smoke/` | e.g. `bero/smoke/` | Missing directory → failure |
| `[dependency-groups].smoke` | consumer `pyproject.toml` | `pytest`, `pytest-playwright` |
| Playwright browser (one-time) | host | `uv run playwright install chromium` |
| Compose `node` service (recommended) | e.g. `compose.dev.yaml` | CI gate runs type-check/build here; avoids host `pnpm install` |

### Recommended

- `[tool.uv] default-groups` includes `"smoke"`
- Root `pytest.ini`: `testpaths = bero/smoke`

## Consumer `pyproject.toml`

```toml
[tool.uv]
default-groups = ["tooling", "dev", "smoke"]

[dependency-groups]
smoke = [
    "pytest>=8.4",
    "pytest-playwright>=0.5",
]
```

```bash
uv sync
uv run playwright install chromium
```

### GitHub Actions (tooling + smoke only)

Host does not need the Django/bero workspace package or a host Node install when compose provides `node`:

```bash
uv sync --frozen --only-group tooling --only-group smoke
uv run playwright install --with-deps chromium
uv run dk ci up -d
uv run tests ci --env ci --no-up --ci
```

Host `uv`/pnpm installs do **not** populate Docker BuildKit cache mounts (`/root/.cache/uv`, pnpm store); those stay inside image builds.
## Writing smoke tests

Location: `{repo_root}/{paths.frontend}/smoke/`. Tooling sets **`SMOKE_FE_URL`**.

Extra pytest args: `uv run tests ci -- -k pwa -vv`

## Bero consumer fast path

```bash
uv run dk dev up -d
uv run tests ci --no-up
uv run tests functional --no-up
uv run tests functional headed --no-up
```

See [bero/README_TESTING.md](https://github.com/catalpainternational/bero/blob/dev-7.4/README_TESTING.md).

## Flags

### `tests ci`

| Flag | Behavior |
|------|----------|
| `--env dev` | Deploy env under `docker/envs/` |
| `--no-up` | Skip `docker compose up -d` |
| `--check-only` | `migrate --check` instead of apply |
| `--fresh-db` | Explicit ephemeral empty DB (already default locally) |
| `--no-fresh-db` | Skip ephemeral empty-DB migrate |
| `--ci` | Assume primary DB empty; skip ephemeral fresh-db |
| `-- …` | Forwarded to pytest |

### `tests functional`

| Flag / mode | Behavior |
|-------------|----------|
| `--env` / `--no-up` | Same as CI |
| `headed` | `--headed --slowmo=250` unless overridden |
| `-- …` | Forwarded to pytest (default `-m elearning`) |

## CI

```yaml
- run: uv run dk dev up -d
- run: uv run tests ci --no-up --ci
```

Use `tests functional` only for content-dependent local runs, not the empty-DB CI job.

## Related docs

| Document | Contents |
|----------|----------|
| [README.md](../README.md) | Install and command overview |
| bero `README_TESTING.md` | Bero usage, elearning / functional tests |
| bero `docs/cursor-rules/bero-deps.mdc` | Where smoke deps must live |
