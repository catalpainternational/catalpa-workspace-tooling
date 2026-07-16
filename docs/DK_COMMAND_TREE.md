# `dk` command tree

Minimal tree for naming review. Environment names (`<env>`) come from `docker/envs/<name>/info.yaml` in each app repo (e.g. `local`, `prod`).

```
dk
├── push                           # build, push, attach CycloneDX SBOMs (--no-sbom to skip)
├── build [SERVICE …]              # build images tagged from git describe
├── clean-images [--apply]         # remove old GHCR package versions (dry-run default)
├── transfer SRC DST               # transfer db and media from one environemnt to another
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
    ├── host [--write]               # verify and show droplet info
    │   └── create [--size …] …      # create droplet
    ├── zabbix                       # devops monitoring
    │   ├── install
    │   ├── enable
    │   ├── disable
    │   ├── restart
    │   └── logs
    ├── ensure_volumes               # utility command 
    ├── storage ensure               # host paths + optional DO volumes + Docker binds
    ├── trust-caddy-cert [--dry-run] # trust Caddy local CA (global local proxy or stack proxy)
    ├── manage <django …>            # access ./manage.py
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
    │   ├── pgrestore
    │   └── restore [--dumps] [--dry-run] [pgBackRest args…]
    │
    ├── compose …                  # explicit passthrough (tab completion); e.g. compose up -d
    ├── docker …                   # passthrough to docker CLI (same DOCKER_HOST/env); e.g. docker volume ls
    └── …                          # implicit compose passthrough (legacy); e.g. up -d
```

## Top-level (no `<env>`)

| Command | Role |
|---------|------|
| `build` | Build compose stack images locally |
| `push` | Build for `linux/amd64`, push to registry, attach CycloneDX SBOMs (`--no-sbom` to skip) |
| `clean-images` | Remove old GHCR package versions (dry-run default; `--apply` to delete) |
| `transfer` | Copy Postgres + `django_media` between two envs |
| `digoc` | DigitalOcean helpers (wraps `doctl`) |
| `proxy` | Machine-wide local dev HTTPS reverse proxy (`*.localdev.temp.build`) |

## `<env>` only

Everything under `<env>` resolves `docker/envs/<env>/info.yaml`, credentials, and `DOCKER_HOST`, then runs on that deployment target.

Special verbs (not plain compose): `info`, `secrets`, `host` / `host create`, `zabbix`, `ensure_volumes`, `storage ensure`, `trust-caddy-cert`, `manage`, `pull_media`, `wipe`, `files` (alias `bkp_files`), `db` (alias `bkp_db`).

`db` / `files` may auto-provision missing WRITE credentials (DigitalOcean Spaces via `doctl` + `s3cmd`, `sops set`); see [README_PGBACKREST.md](../README_PGBACKREST.md) and [README_RESTIC.md](../README_RESTIC.md).

After a successful DB restore (`db restore`, `db pgrestore`, or `transfer` with `--db`), optional `ops.post_db_restore` / `ops.post_metabase_db_restore` hooks in `tooling.yaml` run project follow-ups: `db_psql` (superuser SQL in the `db` container), then `manage_commands` on the web service (default: none). See [README_PGBACKREST.md](../README_PGBACKREST.md#post-restore-hooks-opspost_db_restore--opspost_metabase_db_restore).

Any other first argument is passed to `docker compose` (e.g. `up`, `down`, `ps`, `logs`, `exec`).

`dk <env> docker …` passes remaining args to the `docker` CLI with the same process env as compose (`DOCKER_HOST`, credentials, `COMPOSE_PROJECT_NAME`, …).
