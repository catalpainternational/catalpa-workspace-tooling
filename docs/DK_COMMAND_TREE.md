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
    ├── host [--write]
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

Special verbs (not plain compose): `info`, `secrets`, `host`, `zabbix`, `ensure_volumes`, `trust-caddy-cert`, `manage`, `pull_media`, `wipe`, `bkp_files`, `bkp_db`.

Any other first argument is passed to `docker compose` (e.g. `up`, `down`, `ps`, `logs`, `exec`).
