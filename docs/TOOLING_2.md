# Tooling 2.0: one set of env commands for every runtime

Working plan for the `dev-2.0` branch. It starts from the Fedora CoreOS exploration (#182, #201),
which added a second way to run a stack: rootless Podman quadlets (`runtime: quadlet`) beside
Docker Compose on Ubuntu. 2.0 makes the env commands the same whatever an env runs on. Each
command works differently underneath, according to the env's `info.yaml`.

## Goal

`dk <env> …` means the same thing for a compose env on Ubuntu, a quadlet env on Fedora CoreOS,
and whatever comes next (for example quadlets on a minimal Debian). Where a command can't apply
to a runtime, dk refuses it with a hint saying what to use instead.

## The commands, grouped by what they act on

| Kind | Commands | 2.0 |
|---|---|---|
| The running app | `ps`, `logs`, `exec`, `manage`, `start`/`stop`/`restart`, `db …`, `files …`, `fetch`, `transfer`, `secrets`, `info` | Same verbs and options on every runtime. Done for quadlet in #201 |
| Bringing the host up to date | bare `dk <env>` | Every runtime: compose `up -d`; quadlet: check the render is current, apply it, pull, restart what changed |
| Rendered host files | `render`, `check`, `diff`, `apply` | Plain env verbs, not under `fcos` |
| The host's life | `host create`, `provision`, `ignition`, `destroy`; today also `storage ensure`, `ensure_volumes`, `host logging`, `zabbix install`, `db init`, `*/install-systemd` | Chosen by bootstrap and provider, not runtime |
| Escape hatches | `compose …`, `docker …` | Explicit only: no implicit compose passthrough |

## Decisions

1. **A bare `dk <env>` brings the env up to date.** It's the command people use. On compose it
   stays `up -d`. On quadlet it's today's refusal; in 2.0 it checks that the committed render is
   current, applies it, pulls the images and restarts what changed. That also gives quadlet
   envs a way to deploy a new image under the same tag.
2. **No implicit compose passthrough.** `dk <env> up -d`, `dk <env> config`, etc. stop reaching
   compose; they're refused with a hint (`dk <env> compose up -d`). This frees `dk <env> <verb>`
   to be dk's own verbs. dk has few users, and most of them use a bare `dk <env>`.
3. **`render`/`check`/`diff`/`apply` are plain env verbs.** They're about a rendered host, not
   Fedora CoreOS. `dk <env> fcos …` stays for a while as a deprecated alias.
   `fcos ignition`/`provision` move under `host`, and `fcos secrets push` under `secrets`.
4. **Three settings, not one.** `runtime` (compose | quadlet) chooses how containers run.
   *Bootstrap* (how a host is born and configured: cloud-init, Ignition) and *provider* (where:
   DigitalOcean, none) are separate. Today `fcos` stands for all three. Quadlets on Debian would be
   `runtime: quadlet` with a cloud-init bootstrap, reusing the whole quadlet backend.
5. **One implementation per operation, written over a backend's primitives.** `db restore`,
   `transfer`, `fetch` and the post-restore hooks exist twice today (compose's and
   `fcos/dbdump.py`, `fcos/transfer.py`, `fcos/fetch.py`). They differ only in a few primitives:
   - run a command in a service, with stdin;
   - copy a file into a service;
   - stop and start the writers;
   - the media directory and its owner;
   - push secrets;
   - what to restart after a change.

   A `ComposeBackend` (extracted from today's code) and the `QuadletBackend` implement those, and
   each operation is written once.
6. **Options are dk's.** Verbs take dk's own options and map them per backend. They no longer
   pass compose's flags through unchanged.
7. **Shared secrets live once, in the project's `credentials.yaml`.** `docker/credentials.yaml`
   holds what every env shares (first, the registry bot's login), with its own `.sops.yaml`
   recipients. An env's file overrides it. Rendered hosts read it now; compose envs with M7.

## Milestones

Each one lands on `dev-2.0` with tests, and the project test branches move with it.

| # | What | Size |
|---|---|---|
| M0 | `dev-2.0` = `main` + `feat/182-fcos-render` | done |
| M1 | A bare `dk <env>` on quadlet: check render → apply → pull → restart changed units | done |
| M2 | `render`/`check`/`diff`/`apply` as env verbs; `host ignition`/`host provision`; `secrets push`; `fcos …` deprecated alias | done |
| M2b | `host destroy` (#69): the exact inverse of creating a host. DNS records pointing at it, the droplet, its volumes, `docker_host` and the known_hosts entry, guarded by `allow_destroy` in `info.yaml` | done |
| M3 | Implicit compose passthrough refused with a hint; `compose …` explicit | done |
| M3b | `db backup` defaults to `incr`, as pgBackRest does (it takes a full one when the repository has none); `full`/`incr`/`diff` still accepted. Both runtimes share the parser, so compose envs get it too | done |
| M3c | Project-level `docker/credentials.yaml` (`dk secrets`; an env's file overrides it), and the registry bot's login kept there (stored on the host only for `auto_update` envs; the others log in for each pull): `host provision` logs in from it, `host login` after rotating the token | done |
| M6 | `runtime` / bootstrap / provider split in `info.yaml`; `host create` by bootstrap × provider (with Josh's `host create` work). For a quadlet env, `dk <env> host create` replaces the raw `doctl` steps of a rendered host's provisioning: compile the Ignition, create the data volume (or reuse the one `host destroy --keep-volumes` kept) in the project from `tooling.yaml`, create the droplet with the volume attached and the Ignition as user data, point the DNS records, then `host provision` (with `--no-start` for a restore). After M6, creating, rebuilding and destroying a host need no `doctl` by hand; `doctl` stays for looking around | done. `host create` and `storage.data_volume` (shared with compose). The bootstrap/provider split: decided against for now (see Decided) |
| M4 | Test branches in catalpa-site, swlt_ws2, catalpa_bero and partisipa pinned to `dev-2.0`, each with a checklist | swlt (compose) and catalpa_bero staging (rendered) verified on live hosts; catalpa-site and partisipa to go. First beta `v2.0.0b1` (2026-11) |
| M5 | Backend primitives: `ComposeBackend` extracted, operations rewritten once (restore, transfer, fetch, hooks) | large |
| M5b | Restore-only envs (#204): `restore_from:` in `info.yaml`, per-env read keys created by dk, read and write as two roles, quadlet read mode | `restore_from`, `secrets read-key` and quadlet read mode: done, ahead of M4 (a test host from prod's backups helps test the other projects). Still to do, when a project asks for it: read and write in one env, for a staging that restores prod and also backs itself up (staging content usually isn't backed up; the restored cluster would need a fresh stanza of its own); and on compose, with M5 |
| M7 | Compose hosts rendered too: env files, timers and monitoring config as committed files, applied by `apply`, replacing `db init`, `install-systemd`, `zabbix install`, `storage ensure` | large; may follow 2.0 |

M6 came straight after M3, ahead of its number, so that `host create` replaced the raw `doctl`
steps of the catalpa-site fcos walkthroughs before the other projects' test branches (M4) needed
hosts of their own.

M1–M3 are what users notice. M5 is what makes a third runtime cheap. M7 gives Ubuntu hosts the
same reviewable, diffable desired state as quadlet hosts. It's the biggest change and can follow
2.0.

## Test projects

Each test branch follows [UPGRADING_2.md](UPGRADING_2.md), which is also what every other
project does when it moves to 2.0.

Before `dev-2.0` merges, each of these gets a branch pinned to it and is run through its
checklist:

| Project | Why | Branch, pin | State (2026-11-23) |
|---|---|---|---|
| swlt_ws2 | Newest pin, extra env names (`content`, `qad`); a fixture in the tests. The reference for old compose hosts | `chore/tooling-2.0` (PR #1825, draft, into `dev-0.18`); `dev-2.0` | Local checks, a staging deploy of a beta app tag, qad deployed with `backups: [db, files]`: passed |
| catalpa_bero | Oldest common pin (as jid, ncd and tvi): the upgrade path from far behind. First prod on Fedora CoreOS | `chore/tooling-2.0` from `main`, `feat/staging-fcos` on it; `dev-2.0` | Staging rebuilt as a rendered host: `host create` restored prod's database and media, HTTPS on three origins, HTTP→HTTPS. Prod: 2.0 on Ubuntu, then the bucket consolidation (`docs/DEPLOYMENT.md`), then a cutover runbook and rehearsal |
| catalpa-site | Compose prod on DigitalOcean and the `fcos` quadlet env in one repo | `feat/fcos`; `dev-2.0` at `8aad5b7` | `fcos` env ran a rendered host (OS update window passed). Next: staging becomes a rendered host, the `fcos` env is removed |
| partisipa | Branch `develop`. Its only dk envs are `dev` and `full`, with no deployed env yet: the local commands, then a first deployed env set up from scratch (a candidate for a rendered host) | `develop` at v1.7.0 | Not started (handed to Josh). A rendered host needs the database passwords out of `DATABASE_URL` / `MB_DB_CONNECTION_URI` first |

`host destroy` (#69) is M2b, ahead of the larger milestones, because these test hosts get
destroyed and rebuilt repeatedly, and by hand that leaves DNS records, volumes and known_hosts
entries behind.

Checklist per project: bare `dk <env>` and `dk <env> ps`/`logs`/`exec`/`manage` on staging; `fetch
db`/`fetch media`; `transfer staging dev`; `db info`/`backup`/`restore --dry-run`; `files
snapshots`; `worktree create` and `dk dev` locally; every refused command gives a useful hint.

## Decided

- **What switches an env's backups on: `info.yaml`, not the credentials.** `backups: [db, files]`
  (this env backs itself up) and `restore_from: <env>` (this env restores that env's backups),
  on both runtimes. Credentials keep their read/write names and must match the settings:
  credentials without their setting stop dk, so an upgraded prod env can't quietly stop backing
  up. A render no longer depends on which credentials exist, so `render` → `host provision` →
  `db configure stanza-create` runs in one order on every env. Every project's 2.0 branch adds
  `backups:` to its backed-up envs and `restore_from` to the envs with read credentials.
- **`runtime` decides how a host is set up; no `bootstrap:` or `provider:` settings (M6).**
  `quadlet` is Fedora CoreOS with Ignition, `compose` is Ubuntu with cloud-init, and every host is
  on DigitalOcean. Separate keys would only matter with a second provider or a second way to set up
  a host, and no project needs either, so they'd be settings nobody sets. dk refuses both keys
  rather than ignoring them, refuses an `fcos:` block without `runtime: quadlet`, and quadlet
  `host create` refuses an image slug (tooling.yaml's `digitalocean.image` is Ubuntu's, for compose
  envs). Split them when a second provider or bootstrap arrives: the options are in #206.

## Open questions

- **Restored media and the edge's access to it.** After `files restore`, a quadlet env runs
  `chmod -R o+rX` on every restored volume the edge (Caddy) also mounts. Caddy runs in its own ID
  range, and a compose host's app can write `2750`/`640` (catalpa-site's prod), readable there only
  because its Caddy is root. That fits public media. A project whose edge mounts a volume with a
  private part (files Django serves only after a permission check) would get that part readable
  to every ID range on the host, though Caddy still serves only what it routes. Volumes the edge
  doesn't mount are untouched. Decide before M4 reaches such a project: a per-project switch or
  path list, or Caddy joining the app's group instead.

## Not changing

Local dev (`dk dev`, `worktree`, `proxy`) stays compose. Top-level commands (`build`, `push`,
`transfer`, `fetch`, `cut-release`) keep their names.
