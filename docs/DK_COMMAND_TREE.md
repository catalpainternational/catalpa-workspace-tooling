# `dk` command tree

Minimal tree for naming review. Environment names (`<env>`) come from `docker/envs/<name>/info.yaml` in each app repo (e.g. `local`, `prod`).

```
dk
├── build [SERVICE …]              # stack images (default: all services)
├── push
├── transfer SRC DST
│
├── digoc
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
    ├── info [-e]
    ├── secrets
    ├── host [--write]               # verify droplet + site_origin DNS (DO API + public); print docker_host
    │   └── create [--size …] …      # create droplet, wait, patch docker_host, sync DO DNS; public DNS verify
    ├── zabbix
    │   ├── install
    │   ├── enable
    │   ├── disable
    │   ├── restart
    │   └── logs
    ├── ensure_volumes
    ├── trust-caddy-cert
    ├── manage <django …>
    ├── pull_media
    ├── wipe                       # alias: compose down -v
    │
    ├── bkp_files
    │   ├── install-systemd
    │   ├── init
    │   ├── backup
    │   ├── snapshots
    │   ├── check
    │   ├── stats
    │   └── restore [SNAPSHOT]
    │
    ├── bkp_db
    │   ├── configure [verify | stanza-create]
    │   ├── install-systemd
    │   ├── info
    │   ├── check
    │   ├── version
    │   ├── backup full|incr|diff
    │   ├── pgdump
    │   ├── pgrestore
    │   └── restore
    │
    └── compose …                  # default: up -d (passthrough to docker compose)
```

## Top-level (no `<env>`)

| Command | Role |
|---------|------|
| `build` | Build compose stack images locally |
| `push` | Build for `linux/amd64` and push to registry |
| `transfer` | Copy Postgres + `django_media` between two envs |
| `digoc` | DigitalOcean helpers (wraps `doctl`) |

## `<env>` only

Everything under `<env>` resolves `docker/envs/<env>/info.yaml`, credentials, and `DOCKER_HOST`, then runs on that deployment target.

Special verbs (not plain compose): `info`, `secrets`, `host` / `host create` (doctl: droplet + `site_origin` DNS on DigitalOcean + public resolution; `digitalocean.disabled` for manual hosts; default droplet `{project}-{env}`), `zabbix`, `ensure_volumes`, `trust-caddy-cert`, `manage`, `pull_media`, `wipe`, `bkp_files`, `bkp_db`.

`bkp_db` / `bkp_files` may auto-provision missing WRITE credentials (DigitalOcean Spaces via `doctl` + `s3cmd`, `sops set`); see [README_PGBACKREST.md](../README_PGBACKREST.md) and [README_RESTIC.md](../README_RESTIC.md).

After a successful DB restore (`bkp_db restore`, `bkp_db pgrestore`, or `transfer` with `--db`), optional `ops.post_db_restore.manage_commands` in `tooling.yaml` run via `docker compose exec` on the web service (default: none). See [README_PGBACKREST.md](../README_PGBACKREST.md#post-restore-django-commands-opspost_db_restore).

Any other first argument is passed to `docker compose` (e.g. `up`, `down`, `ps`, `logs`, `exec`).
