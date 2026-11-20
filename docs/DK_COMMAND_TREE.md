# `dk` command tree

Minimal tree for naming review. Environment names (`<env>`) come from `docker/envs/<name>/info.yaml` in each app repo (e.g. `local`, `prod`).

```
dk
├── push                           # build, push, attach CycloneDX SBOMs (--no-sbom to skip)
├── build [SERVICE …]              # build images tagged from git describe
├── clean-images [--apply]         # remove old GHCR package versions (dry-run default)
├── transfer SRC DST [--media-method rsync|tar]  # DB + media (media: rsync default)
├── fetch                          # download production DB dumps and/or media
│   ├── db [-o PATH] [--env NAME] [--only KEY]
│   └── media [--env NAME] …       # same options as native fetch media
│
├── proxy [--dry-run]              # machine-wide local dev HTTPS reverse proxy
│   ├── up                         # start catalpa-local-proxy (Caddy :80/:443); CA persisted on host
│   ├── down                       # stop/remove catalpa-local-proxy (host-persisted CA kept)
│   ├── status                     # running? live sites (host -> upstream, project/env)
│   └── trust                      # trust "Catalpa Local Dev Root (<machine>)" CA once (macOS/Linux)
│
├── cut-release                    # cut a final or beta v* tag (dry-run default)
│   ├── final                      # on dev-*: merge main, tag v*, push; suggest next-branch
│   └── beta [W] [--tag TAG]       # on named branch: tag v*.beta.W (-C PATH, --execute)
│
├── next-branch <bump|dev-*>       # open next dev-* from final v* tip (--set-default)
│
├── worktree                       # isolated git worktrees for local dk dev
│   ├── create <slug>              # .worktrees/<slug> + overlay + AGENTS.local.md; seeds DB + media (--no-seed); --up → proxy + stack
│   ├── up [slug]                  # ensure proxy + remapped compose up -d
│   ├── down [slug]                # compose down (keeps volumes)
│   ├── restart [slug] [--service …]
│   ├── logs [slug] [-f] [SERVICE …]
│   ├── status [slug]              # overlay + stack status + compose ps
│   ├── context [slug] [--json]    # agent identity; refreshes AGENTS.local.md
│   ├── list
│   ├── info [slug]
│   ├── seed [slug] [--db|--media] # copy main dev DB + media storage (slug from main checkout)
│   ├── remove <slug> [--wipe] [--keep-stack]  # down + git worktree remove; --wipe also drops
│   │                              # volumes (external: included); --keep-stack leaves it up
│   └── claude-hook create|remove  # Claude Code WorktreeCreate/WorktreeRemove hook (JSON on stdin)
│
├── context [--env NAME] [--json] [--status]  # every env's origins, compose project, local/remote;
│                                  # worktree branch + base; config only unless --status
│
# Global (before subcommand):
#   dk --worktree <slug> | -W <slug>  …   # target .worktrees/<slug> without cd
│
├── digoc                          # access to doctl (probably removing this for access via dk)
│   ├── auth                        
│   │   ├── init
│   │   ├── list
│   │   ├── remove                 # → host doctl
│   │   └── switch                 # → host doctl
│   ├── cloud-config
│   │   └── print
│   ├── projects
│   │   └── list
│   └── droplets
│       ├── list
│       ├── create
│       └── suggest-env
│
└── <env>  [--dry-run] [-y] [--tag TAG]  …
    ├── info [-e]                    # see or edit env info
    ├── secrets                      # edit env encrypted credentials via SOPS
    │   └── push [NAME…] [--restart] # rendered host: sops → podman secrets over SSH stdin
    ├── dc-backup                    # TLS + Garage/Caddy on dc_backup_docker_host
    ├── host [--write]               # verify and show droplet info
    │   ├── create [--size …] …      # create droplet
    │   ├── destroy [--dry-run] [--keep-dns|--keep-volumes|--keep-docker-host]  # DNS, droplet (clean shutdown), volumes; digitalocean.allow_destroy only
    │   ├── logging [--apply] [--target app|backup]  # Docker log rotation in daemon.json
    │   ├── ignition -o FILE         # rendered host: compile host.bu (Butane) → droplet user data
    │   └── provision [--no-start]   # rendered host: check files, push secrets, registry login, start
    ├── zabbix                       # devops monitoring
    │   ├── install
    │   ├── enable
    │   ├── disable
    │   ├── restart
    │   └── logs
    │   (runtime: quadlet envs: a bare `dk <env>` applies the committed render, pulls and
    │    restarts what changed; logs, ps, exec, manage, restart/stop/start go to the host's
    │    systemd/podman over SSH; other compose verbs are refused — docs/FCOS.md)
    ├── render                       # rendered host (runtime: quadlet): write generated/fcos/ (commit it)
    ├── check                        # committed render == fresh render; secrets required
    ├── diff [--names-only]          # host files/dirs/timers/secret names vs the render
    ├── apply [--no-restart] [-y] [--force] # install what differs (incl. SSH keys); restart affected units
    ├── fcos …                       # deprecated spelling of the five above, with a note
    ├── ensure_volumes               # utility command 
    ├── storage ensure               # host paths + optional DO volumes + Docker binds
    ├── trust-caddy-cert [--dry-run] # trust Caddy local CA (global local proxy or stack proxy)
    ├── manage [--user U] <django …> # ./manage.py as stack.manage_user (or U)
    ├── pull_media                   # volume → host dir (tar)
    ├── wipe                         # alias: compose down -v
    │
    ├── files                        # restic backups + host media → volume (alias: bkp_files)
    │   ├── push [--source DIR] [--method rsync|tar]
    │   ├── install-systemd
    │   ├── init
    │   ├── backup
    │   ├── snapshots
    │   ├── check
    │   ├── stats
    │   └── restore [SNAPSHOT]
    │
    ├── db                           # pgBackRest backups (alias: bkp_db)
    │   ├── init [--install-systemd [--dry-run] [--enable]]
    │   ├── configure [verify | stanza-create]
    │   ├── install-systemd
    │   ├── info
    │   ├── check
    │   ├── version
    │   ├── backup full|incr|diff
    │   ├── pgdump
    │   └── restore [--dumps] [--file PATH] [--dry-run] [pgBackRest args…]
    │
    ├── compose …                  # explicit passthrough (tab completion); e.g. compose up -d
    ├── docker …                   # passthrough to docker CLI (same DOCKER_HOST/env); e.g. docker volume ls
    └── …                          # implicit compose passthrough (legacy); e.g. up -d
```

## Top-level (no `<env>`)

| Command | Role |
|---------|------|
| `build` | Build compose stack images locally |
| `push` | Build for `images.yaml` `deploy_platform` (default `linux/amd64`, injected as `STACK_PLATFORM`), push to registry, attach CycloneDX SBOMs (`--no-sbom` to skip) |
| `clean-images` | Remove old GHCR package versions (dry-run default; `--apply` to delete) |
| `transfer` | Copy Postgres + `django_media` between two envs (media: rsync `--delete` by default; `--media-method tar` for full archive; first remote sync is still roughly full-size, repeats are incremental). Media path resolved via merged `compose config` (binds and `/django_media` supported). |
| `digoc` | DigitalOcean helpers (wraps `doctl`) |
| `proxy` | Machine-wide local dev HTTPS reverse proxy (`*.localdev.temp.build`) |
| `cut-release` | Cut final or beta `v*` tags (`final` / `beta`; see [CUT_RELEASE.md](CUT_RELEASE.md)) |
| `next-branch` | Open next `dev-*` line from a final `v*` tip (see [CUT_RELEASE.md](CUT_RELEASE.md)) |
| `worktree` | Isolated git worktrees under `.worktrees/` for parallel local `dk dev` (see [WORKTREES.md](WORKTREES.md)). Global `--worktree`/`-W <slug>` retargets any `dk` command from the main checkout. |
| `context` | What an agent needs before touching the stack: every origin, compose project and web service per env, local vs remote, and inside a worktree its branch and what it was cut from. Reads config only (no SOPS, ssh or docker); `--status` adds `docker ps` for local envs. `--json` output carries `"schema": 1`. Works in the main checkout and in worktrees. |

## `<env>` only

Everything under `<env>` resolves `docker/envs/<env>/info.yaml`, credentials, and `DOCKER_HOST`, then runs on that deployment target.

Special verbs (not plain compose): `info`, `secrets`, `dc-backup`, `host` / `host create` / `host destroy` / `host logging`, `zabbix`, `render` / `check` / `diff` / `apply` / `host ignition` / `host provision` / `secrets push` (rendered hosts; `fcos …` is the deprecated spelling), `ensure_volumes`, `storage ensure`, `trust-caddy-cert`, `manage`, `pull_media`, `wipe`, `files` (alias `bkp_files`), `db` (alias `bkp_db`).

`db` / `files` may auto-provision missing WRITE credentials (DigitalOcean Spaces via `doctl` + `s3cmd`, `sops set`); see [README_PGBACKREST.md](../README_PGBACKREST.md) and [README_RESTIC.md](../README_RESTIC.md).

After a successful DB restore (`db restore`, `transfer` with `--db`, or the database step of `worktree seed`), optional `ops.post_db_restore` / `ops.post_metabase_db_restore` hooks in `tooling.yaml` run project follow-ups: `db_psql` (superuser SQL in the `db` container), then `manage_commands` on the web service (default: none). See [README_PGBACKREST.md](../README_PGBACKREST.md#post-restore-hooks-opspost_db_restore--opspost_metabase_db_restore).

Any other first argument is passed to `docker compose` (e.g. `up`, `down`, `ps`, `logs`, `exec`).

`dk <env> docker …` passes remaining args to the `docker` CLI with the same process env as compose (`DOCKER_HOST`, credentials, `COMPOSE_PROJECT_NAME`, …).
