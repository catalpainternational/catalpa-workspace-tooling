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
| `dk context [--json]` | Every env's origins and compose project, plus the worktree's `base_ref`/`base_commit`; also works in the main checkout |
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

`create` also records what a new branch was cut from: `base_ref` (the branch or tag name;
`--base-branch` defaults to `HEAD`, which resolves to the main checkout's current branch) and
`base_commit`. Neither is recorded when `--branch` names a branch that already exists, or for
worktrees created before this was added. `base_ref` is omitted when `HEAD` was detached.
`dk context` and `dk worktree context` report both.
- Rewrites role / Caddy origins (`admin.*`, `stats.*`, etc.) from the new primary
- **Does not** remap remote envs (`docker_host: ssh://…`)

`dk full`, `staging`, `prod` are unchanged. Media is seeded between the two stacks' **media storages** — the named volume a Docker project serves from, or its host bind — so the copy reaches the stack rather than a host directory the stack never mounts.

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
dk worktree remove <slug> [--wipe] [--keep-stack] [--dry-run] [-y]
```

- **create** — worktree + overlay + `AGENTS.local.md` + submodule init; seeds DB + media from the main checkout's base env by default (`--no-seed` to skip). Pass **`--up`** to proxy + compose up after create.
- **up / down / restart / logs** — remapped compose lifecycle via the worktree overlay (volumes kept on `down`).
- **status / context** — stack status (`running` / `stopped` / `unknown`) and agent-oriented identity.
- **seed** — re-run `pg_dump` / restore from the main checkout’s base env compose project, plus a copy of that stack's media storage (volume or bind) onto the worktree's. The media copy replaces whatever the destination held, and takes Wagtail originals and renditions together from the same source stack as the dump, so image rows and files stay consistent. A source with no media is skipped, not an error; the final line reports what each step did.
- **remove** — `compose down` for the remapped project, then git worktree remove. **Volumes are
  kept**; the command prints how to reclaim them. Since 1.4.1: before that, the stack was left
  running for a project name that no longer had a checkout.
- **remove --wipe** — the same, but `compose down -v`, then removes the `external:` volumes
  Compose will not touch (`postgres_data`, `django_media`, `caddy_data`, `postgres_conf`,
  `pgbackrest_conf`). Destroys that worktree’s database and uploads — there is no undo. Built
  images tagged with the worktree’s project name are left in place.
- **remove --keep-stack** — the pre-1.4.1 behaviour: retire the checkout and leave the stack up.
  Mostly useful when the Docker daemon is not running and you want the checkout gone anyway.

If the teardown fails, the checkout is deliberately **kept** so the command can be re-run —
removing it would strand whatever could not be cleared under a name nothing can look up.

## Agents

A project that lists `worktrees` under `agents.rules` in `tooling.yaml` opts in to
**worktree-first development**: agents do every development task in a `dk worktree`, never in
the main checkout. `uv run setup-agents` installs the always-on `worktrees` rule (Cursor and
Claude Code) and Claude Code's worktree hooks below.

### Claude Code worktrees

Claude Code makes its own worktrees with a plain `git worktree add` under `.claude/worktrees/`.
That happens for `claude --worktree <name>`, EnterWorktree, and subagents with
`isolation: worktree`. Such a worktree has no overlay, so it would share the main checkout's
stack. With the `worktrees` rule, setup-agents adds two hooks to `.claude/settings.json`, and
Claude Code runs them instead:

| Hook | Runs | Does |
|------|------|------|
| `WorktreeCreate` | `dk worktree claude-hook create` | Asks `origin` for its default branch (`git ls-remote --symref`, since the local `origin/HEAD` can be stale), fetches it, and runs `dk worktree create <name> --base-branch origin/<default> --no-seed`. The new `.worktrees/<slug>` path is the only output on stdout; everything else goes to stderr. |
| `WorktreeRemove` | `dk worktree claude-hook remove` | Runs `dk worktree remove <slug> -y`, which stops the stack and keeps its volumes. Then `git branch -d` on the worktree's branch, which keeps it unless it is merged or fully pushed. It refuses any path outside `.worktrees/`. |

Some details:

- The hooks run `uv run dk` in `$CLAUDE_PROJECT_DIR`, the main checkout, using that checkout's
  installed tooling version.
- No seed and no stack keeps parallel subagents cheap. Run `dk worktree seed <slug> -y` and
  `dk worktree up <slug>` when the task needs the running app.
- Claude Code doesn't remove subagents' worktrees. Remove them with `dk worktree remove`.
- If the stack can't be stopped (for example, Docker isn't running), remove fails and leaves
  the worktree in place, as `dk worktree remove` does.

Without the rule: prefer `dk worktree create --up` for parallel feature work. Each worktree gets `AGENTS.local.md` with slug, origins, and command hints. Use `dk worktree context --json` from automation. Control stacks with `dk worktree up/down` or `dk --worktree <slug> …` from the main checkout. Never use worktree overlays for remote deploy envs.

See also [AGENTS_AND_SECRETS.md](AGENTS_AND_SECRETS.md) and [DK_COMMAND_TREE.md](DK_COMMAND_TREE.md).
