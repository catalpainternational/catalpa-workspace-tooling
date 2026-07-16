# Closed-DC Garage backup (`dc-backup`)

Deploy a single-node [Garage](https://garagehq.deuxfleurs.fr/) S3 + Caddy TLS on a separate **DC backup host**, and wire app hosts so pgBackRest / restic verify TLS without editing consumer `compose.yml`.

Distinct from DigitalOcean Spaces auto-provision (`pgbr_s3_*` / `restic_write_*` toward Spaces).

**Related docs**

- [README_PGBACKREST.md](README_PGBACKREST.md) — path-style + `repo1-storage-ca-file`
- [README_RESTIC.md](README_RESTIC.md) — `AWS_CA_BUNDLE` on restic docker runs
- [docs/AGENTS_AND_SECRETS.md](docs/AGENTS_AND_SECRETS.md) — keep `dc-backup*.yaml` out of agent context

## Topology

```mermaid
flowchart LR
  app[app_docker_host]
  bkp[dc_backup_docker_host]
  app -->|"HTTPS_S3"| caddy[Caddy_:443]
  caddy --> garage[Garage_127.0.0.1:3900]
  garage --> disk["/var/lib/garage"]
```

## `info.yaml`

```yaml
docker_host: ssh://root@172.16.92.27
dc_backup_docker_host: ssh://root@172.16.92.28
# env:
#   DOCKER_ADD_HOST: "s3.backup.internal:172.16.92.28"  # only if using a hostname endpoint
#   DC_BACKUP_CA_FILE: /custom/path/ca.crt               # optional override
```

## Runtime knobs

| Key | Purpose |
|-----|---------|
| `DOCKER_ADD_HOST` | Optional `hostname:IPv4` list. Omit when the S3 endpoint is a bare IP. |
| `DC_BACKUP_CA_FILE` | Optional. When unset and `dc-backup-tls.yaml` exists, defaults to `{ops.config_dir}/tls/dc-backup-ca.crt`. |

In-container CA path: `/etc/ssl/dc-backup-ca/ca.crt`.

## Host layout (DC backup machine)

| Path | Role |
|------|------|
| `/opt/garage/docker-compose.yml` | Garage + Caddy |
| `/etc/garage.toml` | Garage config (**file**) |
| `/etc/garage/Caddyfile` | TLS reverse proxy (**file**) |
| `/etc/garage/tls/{ca,server}.{crt,key}` | From `dc-backup tls install` |
| `/var/lib/garage/{meta,data,caddy-data}` | Persistence |

## SOPS

| File | Contents |
|------|----------|
| `docker/envs/<env>/dc-backup-tls.yaml` | CA + server PEMs + SANs |
| `docker/envs/<env>/dc-backup.yaml` | `garage_rpc_secret`, `garage_admin_token`, optional images |

App S3 access keys stay in `credentials.yaml`.

## CLI

```bash
dk prod dc-backup tls issue --ip 172.16.92.28 [--dns NAME] [--days 825] [--force]
dk prod dc-backup tls install
dk prod dc-backup tls status [--check-remote]

dk prod dc-backup bootstrap [--force]
dk prod dc-backup install [--up] [--dry-run]
dk prod dc-backup status [--check-remote]
```

## Consumer flow

1. Set `dc_backup_docker_host` + app `docker_host`.
2. `dk <env> dc-backup tls issue --ip …` → `tls install`
3. `dk <env> dc-backup bootstrap` → `install --up`
4. Once on the backup host (document for ops):

```bash
alias garage='docker exec garage /garage'
garage status
# garage layout assign -z tic -c 300G <node_id_prefix>
# garage layout apply --version 1
garage bucket create indmo-backups
garage key create indmo-prod-backup
garage bucket allow --read --write --owner indmo-backups --key indmo-prod-backup
garage key info indmo-prod-backup
```

5. Put write keys in `credentials.yaml` (path-style, `verify_tls: y`, IP or hostname endpoint).
6. Recreate `db`; run `db` / `files` backups.

Endpoint can be a **bare IP** (IP SAN on the cert) or a **hostname** (`dc-backup tls issue --dns …` plus `DOCKER_ADD_HOST=name:ip` in `info.yaml` `env:` so containers resolve it without DC DNS). Host `/etc/hosts` alone is not enough inside Docker.
