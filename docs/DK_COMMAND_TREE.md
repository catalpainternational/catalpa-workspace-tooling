# `dk` command tree

Minimal tree for naming review. Environment names (`<env>`) come from `docker/envs/<name>/info.yaml` in each app repo (e.g. `local`, `prod`).

```
dk
├── push                           # build and push images tagged from git describe
├── build [SERVICE …]              # build images tagged from git describe
├── transfer SRC DST               # transfer db and media from one environemnt to another
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
    ├── trust-caddy-cert [--dry-run] # macOS: trust Caddy local CA (stack.services.proxy)
    ├── manage <django …>            # access ./manage.py
    ├── pull_media                   # volume → host dir (tar)
    ├── wipe                         # alias: compose down -v
    │
    ├── bkp_files                    # restic backups + host media → volume
    │   ├── push [--source DIR] [--method rsync|tar]
    │   ├── install-systemd
    │   ├── init
    │   ├── backup
    │   ├── snapshots
    │   ├── check
    │   ├── stats
    │   └── restore [SNAPSHOT]
    │
    ├── bkp_db                       # control pgbackrest backups
    │   ├── init [--install-systemd [--dry-run] [--enable]]
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
    ├── compose …                  # explicit passthrough (tab completion); e.g. compose up -d
    └── …                          # implicit compose passthrough (legacy); e.g. up -d
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

Special verbs (not plain compose): `info`, `secrets`, `host` / `host create` (doctl: droplet + `site_origin` DNS on DigitalOcean + public resolution; `digitalocean.disabled` for manual hosts; default droplet `{project}-{env}`), `zabbix`, `ensure_volumes`, `trust-caddy-cert` (macOS only; uses env compose file + `stack.services.proxy`), `manage`, `pull_media`, `wipe`, `bkp_files`, `bkp_db`.

`bkp_db` / `bkp_files` may auto-provision missing WRITE credentials (DigitalOcean Spaces via `doctl` + `s3cmd`, `sops set`); see [README_PGBACKREST.md](../README_PGBACKREST.md) and [README_RESTIC.md](../README_RESTIC.md).

After a successful DB restore (`bkp_db restore`, `bkp_db pgrestore`, or `transfer` with `--db`), optional `ops.post_db_restore.manage_commands` in `tooling.yaml` run via `docker compose exec` on the web service (default: none). See [README_PGBACKREST.md](../README_PGBACKREST.md#post-restore-django-commands-opspost_db_restore).

Any other first argument is passed to `docker compose` (e.g. `up`, `down`, `ps`, `logs`, `exec`).
