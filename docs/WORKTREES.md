# Isolated git worktrees (`dk worktree`)

Create parallel git checkouts for feature or maintenance work, each with its own Docker Compose project, Postgres volumes, and `*.localdev.temp.build` hostnames — while keeping the familiar `dk dev` command.

## Quick start

From the **main** checkout (no `cd` / direnv required for day-to-day `dk`):

```bash
uv run dk worktree create my-feature --up
# or: create then up separately
uv run dk worktree create my-feature
uv run dk worktree up my_feature
```

Open `https://{project}-dev-{slug}.localdev.temp.build` (printed by `create`). Optional: `cd .worktrees/<slug> && uv sync` when editing that tree; **File → Add Folder to Workspace** in Cursor/VS Code.

`create --up` ensures the shared local proxy and runs remapped compose `up -d`. It does **not** run `manage migrate` — run that when needed:

```bash
uv run dk --worktree my_feature dev manage migrate
```

## Control from the main checkout

| Flag / command | Purpose |
|----------------|---------|
| `dk --worktree <slug> …` / `dk -W <slug> …` | Retarget any `dk` command at `.worktrees/<slug>` (process `chdir` only — no shell direnv) |
| `dk worktree up/down/restart/logs/status` | Lifecycle for the remapped stack without spelling `dev up -d` |
| `dk worktree context [--json]` | Agent/human identity (refreshes `AGENTS.local.md`) |
| `dk worktree seed <slug>` | Re-seed that worktree from main without entering it |

Examples:

```bash
uv run dk --worktree onboarding dev manage migrate
uv run dk worktree up onboarding
uv run dk worktree logs onboarding -f
uv run dk worktree context onboarding --json
uv run dk worktree seed onboarding -y
```

## Layout

| Path | Role |
|------|------|
| `.worktrees/<slug>/` | Git worktree (gitignored from the parent) |
| `.catalpa-worktree.yaml` | Gitignored overlay at the worktree root |
| `AGENTS.local.md` | Gitignored agent context (written on create; refreshed on `up` / `context`) |

`create` appends `.worktrees/`, `.catalpa-worktree.yaml`, and `AGENTS.local.md` to the repo `.gitignore` when missing — commit that change once per consumer. When `.gitmodules` is present, **`create` initializes each submodule shallowly (`--depth 1`)**, preferring **`--reference` from the matching path in the main checkout** (e.g. `bero/` already present locally — fast / offline). If that path is missing, it falls back to a remote fetch. Use `--full-submodules` for a full history clone, or `--no-submodules` to skip.

Slug sanitization: hyphens become underscores in the directory and compose project (`my-feature` → `my_feature`).

## Overlay behavior

When `.catalpa-worktree.yaml` is present and you run `dk <base_env>` (default `base_env: dev`):

- Remaps `COMPOSE_PROJECT_NAME` → `{compose_project_default}_dev_{slug}`
- Remaps `site_origin` → `https://{project}-dev-{slug}.{site_origin_base}`
- Rewrites role / Caddy origins (`admin.*`, `stats.*`, etc.) from the new primary
- **Does not** remap remote envs (`docker_host: ssh://…`)

`dk full`, `staging`, `prod` are unchanged. Host `media/` for `dk dev` is already per-checkout (bind mount); seed copies that tree from the main checkout.

**Proxy:** `dk worktree up` ensures the shared reverse proxy and registers remapped routes on compose `up`. Main and worktree stacks get distinct hostnames and upstreams.

## Commands

```
dk --worktree <slug> | -W <slug>   # global: target a worktree for the rest of the argv
dk worktree create <slug> [--branch NAME] [--base-branch REF] [--base-env dev] [--up] [--no-submodules] [--full-submodules] [--no-seed] [--dry-run]
dk worktree up [slug] [--dry-run]
dk worktree down [slug] [--dry-run]
dk worktree restart [slug] [--service NAME …] [--dry-run]
dk worktree logs [slug] [-f] [SERVICE …] [--dry-run]
dk worktree status [slug]
dk worktree context [slug] [--json]
dk worktree list
dk worktree info [slug]
dk worktree seed [slug] [--db|--media] [--from DIR] [--dry-run] [-y]
dk worktree remove <slug> [--wipe] [--dry-run] [-y]
```

- **create** — worktree + overlay + `AGENTS.local.md` + submodule init; seeds DB + host media from the main checkout's base env by default (`--no-seed` to skip). Pass **`--up`** to proxy + compose up after create.
- **up / down / restart / logs** — remapped compose lifecycle via the worktree overlay (volumes kept on `down`).
- **status / context** — stack status (`running` / `stopped` / `unknown`) and agent-oriented identity.
- **seed** — re-run `pg_dump` / restore from the main checkout’s base env compose project, plus host `media/` copy.
- **remove** — git worktree remove only (stack may keep running).
- **remove --wipe** — also `compose down -v` for the remapped project (destroys that worktree’s Docker volumes).

## Agents

Prefer `dk worktree create --up` for parallel feature work. Each worktree gets `AGENTS.local.md` with slug, origins, and command hints. Use `dk worktree context --json` from automation. Control stacks with `dk worktree up/down` or `dk --worktree <slug> …` from the main checkout. Never use worktree overlays for remote deploy envs.

See also [AGENTS_AND_SECRETS.md](AGENTS_AND_SECRETS.md) and [DK_COMMAND_TREE.md](DK_COMMAND_TREE.md).
