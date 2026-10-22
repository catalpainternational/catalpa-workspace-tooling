---
name: dk-stack
description: How to bring up, reach and drive a catalpa-workspace-tooling project's local Docker stack (`uv run dk dev`), in the main checkout or a `dk worktree`. Use whenever asked to run, start, preview or screenshot the app, to run Django management commands, or to confirm a change in the running app. A project's own `run-app` skill, if present, adds what is specific to that project and wins where they disagree.
---

# Running a catalpa-workspace-tooling stack

This project's stack is launched through **catalpa-workspace-tooling**, not
with a bare `docker compose up` or `manage.py runserver`. Its shape — compose
files, services, hostnames — is configured in `tooling.yaml` and
`docker/envs/<env>/info.yaml`. Read values from there or from the tooling
rather than guessing them.

**Local envs only.** `dk context` (below) marks each env `"location": "local"`
or `"remote"`. A remote env (normally `staging`, `prod`) is a server: do not
run any `dk` command against it, and do not run fetches that read from it
(`native fetch`, `dk <remote> db pgdump`), unless the user confirms target and
action. See the `remote-environments` agent rule (`.cursor/rules/remote-environments.mdc`
for Cursor, `.claude/rules/remote-environments.md` for Claude Code).

## Which checkout are you in?

Every name — compose project, container names, URLs — differs between the main
checkout and each worktree. Ask the tooling, before anything else:

```bash
uv run dk context --json                 # main checkout or worktree
uv run dk -W <slug> context --json       # a worktree, from the main checkout
uv run dk context --env dev --status     # one env, plus which of its containers are running
```

It only reads config (no secrets, ssh or docker unless `--status`), so it is
safe to run first. Read from it:

- `checkout`: `"main"` or `"worktree"`.
- `envs[]`: per env, `origins` (every hostname it serves; use these, not a
  guessed URL), `compose_project`, `location` (`local` / `remote`),
  `local_proxy`, and `worktree_overlay` (true when this checkout's worktree
  remaps that env).
- `web_service`: the compose service `dk … manage` execs into.
- `worktree` (null in the main checkout): `slug`, `branch`, `head_commit`, and
  `base_ref` / `base_commit`, what the worktree was cut from. `branch` is the
  worktree's own branch, not its base. Both base fields are null for worktrees
  created before the tooling recorded them, or from a branch that already
  existed. If `base_ref` is behind its upstream, the worktree started stale.

`uv run dk worktree context --json` also reports a worktree's `status` and
whether it was `seeded`. It refreshes the gitignored `AGENTS.local.md`, which
has the same facts for a human.

If `dk context` is an unknown command, the project's tooling is older than
it: use `dk worktree context --json` in a worktree, and `site_origin` in
`docker/envs/<env>/info.yaml` in the main checkout (`dk dev --dry-run` prints
only the first origin).

## Start, wait, stop

```bash
uv run dk dev up -d                      # main checkout
uv run dk --worktree <slug> dev up -d    # a worktree, from the main checkout or inside it
uv run dk dev ps
uv run dk dev logs -f <service>
uv run dk dev down                       # keeps volumes
```

Everything after `dk <env>` that is a compose verb is passed to
`docker compose`. Since 1.6.0 an unknown word is refused rather than starting
the stack.

A cold start can take minutes (image builds, frontend installs). Not every
service has a healthcheck, so wait for the URL to answer rather than for
`ps` to look right:

```bash
curl -sk -o /dev/null -w '%{http_code}\n' <origin>/   # an origin from dk context
```

If the browser rejects the certificate, the local CA needs trusting once per
machine: `uv run dk proxy trust` (sudo — ask the user). `dk proxy ca` is for
LAN devices, not this machine.

## Worktrees

```bash
uv run dk worktree create <slug> --base-branch origin/<trunk> --up
uv run dk worktree remove <slug>         # stops the stack, keeps volumes; prints the volume rm
```

- `create` seeds the database and media from the main checkout's `dev` stack,
  so the worktree starts with a **copy of main's data**, not trunk's seeders.
  It does not migrate: run `uv run dk --worktree <slug> dev manage migrate`.
- `--up` brings up the new worktree's stack, not main's (fixed in 1.4.1).
- `remove --wipe` destroys that worktree's database. Confirm with the user.
- `--base-branch` defaults to `HEAD`, the main checkout's current branch,
  however stale it is. Pass `--base-branch origin/<trunk>` explicitly.
- The overlay gives a worktree **one** primary hostname
  (`<project>-dev-<slug>.<base>`, plus role hosts such as `admin.`), and does
  **not** remap ports a compose file publishes on the host. A project that
  serves several sites loses all but the first in a worktree; compare
  `dk context` `origins` in the worktree with the main checkout's. A project
  that publishes a host port (a debugger, a database) may not work in a
  worktree either. Check its `run-app` skill.
- `tests ci`, `tests guest` and `tests functional` refuse to run inside a
  worktree (since 1.5.0) and say to run them from the main checkout.

## Management commands

```bash
uv run dk dev manage <command> [args...]                  # execs into the running web service
uv run dk --worktree <slug> dev manage <command> [args...]
uv run native manage <command> [args...]                  # host-only setups (no Docker)
```

Not a bare `python manage.py`: that runs against whatever settings the host
shell happens to have.

## Showing that it works

If a headless browser driver is available, load the affected page at the
stack's origin and check the console. If none is, say so rather than claiming
the UI was verified: a `200` from `curl`, or a passing API call, does not show
the page rendered.

## Tests

`uv run tests --help` lists what this project has wired up: `backend`
(pytest), `frontend`, `ci` (the CI gate), `smoke`, `guest`, `functional`. A
project's `run-app` skill should say which ones matter and where they differ
from CI.
