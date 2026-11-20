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

## Milestones

Each one lands on `dev-2.0` with tests, and the project test branches move with it.

| # | What | Size |
|---|---|---|
| M0 | `dev-2.0` = `main` + `feat/182-fcos-render` | done |
| M1 | A bare `dk <env>` on quadlet: check render → apply → pull → restart changed units | done |
| M2 | `render`/`check`/`diff`/`apply` as env verbs; `host ignition`/`host provision`; `secrets push`; `fcos …` deprecated alias | done |
| M2b | `host destroy` (#69): the exact inverse of creating a host. DNS records pointing at it, the droplet, its volumes, `docker_host` and the known_hosts entry, guarded by `allow_destroy` in `info.yaml` | done |
| M3 | Implicit compose passthrough refused with a hint; `compose …` explicit | small |
| M3b | `db backup` defaults to `incr`, as pgBackRest does (it takes a full one when the repository has none); `full`/`incr`/`diff` still accepted. Both runtimes share the parser, so compose envs get it too | small |
| M4 | Test branches in catalpa-site, swlt_ws2, catalpa_bero and partisipa pinned to `dev-2.0`, each with a checklist | ongoing from here |
| M5 | Backend primitives: `ComposeBackend` extracted, operations rewritten once (restore, transfer, fetch, hooks) | large |
| M5b | Restore-only envs (#204): `restore_from:` in `info.yaml`, per-env read keys created by dk, read and write as two roles, quadlet read mode | medium |
| M6 | `runtime` / bootstrap / provider split in `info.yaml`; `host create` by bootstrap × provider (with Josh's `host create` work) | medium |
| M7 | Compose hosts rendered too: env files, timers and monitoring config as committed files, applied by `apply`, replacing `db init`, `install-systemd`, `zabbix install`, `storage ensure` | large; may follow 2.0 |

M1–M3 are what users notice. M5 is what makes a third runtime cheap. M7 gives Ubuntu hosts the
same reviewable, diffable desired state as quadlet hosts. It's the biggest change and can follow
2.0.

## Test projects

Before `dev-2.0` merges, each of these gets a branch pinned to it and is run through its
checklist:

| Project | Why | Current pin |
|---|---|---|
| catalpa-site | Compose prod on DigitalOcean and the `fcos` quadlet env in one repo | `dev-2.0` (#1010) |
| swlt_ws2 | Newest pin, extra env names (`content`, `qad`); a fixture in the tests | v1.7.0 |
| catalpa_bero | Oldest common pin (as jid, ncd and tvi): the upgrade path from far behind | v1.3.3 |
| partisipa | Branch `develop`. Its only dk envs are `dev` and `full`, with no deployed env yet: the local commands, then a first deployed env set up from scratch (a candidate for a rendered host) | v1.7.0 |

`host destroy` (#69) is M2b, ahead of the larger milestones, because these test hosts get
destroyed and rebuilt repeatedly, and by hand that leaves DNS records, volumes and known_hosts
entries behind.

Checklist per project: bare `dk <env>` and `dk <env> ps`/`logs`/`exec`/`manage` on staging; `fetch
db`/`fetch media`; `transfer staging dev`; `db info`/`backup`/`restore --dry-run`; `files
snapshots`; `worktree create` and `dk dev` locally; every refused command gives a useful hint.

## Open questions

- **What switches a rendered env's backups on.** Today it's which `pgbr_s3_write_*` /
  `restic_write_*` names `credentials.yaml` has (a render reads names, never values). So a new env
  needs credentials before its render has backup units, and removing stale credentials changes
  the render. Keying it on configuration (`ops.pgbackrest`/`ops.restic`, or an env setting)
  instead would let `render` → `provision` → `db configure stanza-create` run in one order on
  every env.

## Not changing

Local dev (`dk dev`, `worktree`, `proxy`) stays compose. Top-level commands (`build`, `push`,
`transfer`, `fetch`, `cut-release`) keep their names.
