# Private backup TLS (CA, extra hosts, cert lifecycle)

Closed-network S3 (Garage / MinIO / Caddy TLS) with **TLS verify on**, without editing consumer `compose.yml`. Tooling injects optional Docker `extra_hosts` and a host CA bind-mount, and can **issue**, **SOPS-store**, and **install** a private CA + server certificate.

**Related docs**

- [README_PGBACKREST.md](README_PGBACKREST.md) — `repo1-storage-ca-file` and S3 URI style
- [README_RESTIC.md](README_RESTIC.md) — `AWS_CA_BUNDLE` on restic docker runs
- [docs/AGENTS_AND_SECRETS.md](docs/AGENTS_AND_SECRETS.md) — keep `backup-tls.yaml` out of agent context

## Runtime knobs (`info.yaml` `env:` or credentials)

| Key | Purpose |
|-----|---------|
| `DOCKER_ADD_HOST` | Optional `hostname:IPv4` list (comma/space separated). Used when the S3 URL is a hostname that is not in DC DNS. Omit when the S3 endpoint is a bare IP. |
| `BACKUP_CA_FILE` | Optional override. When unset and `docker/envs/<env>/backup-tls.yaml` exists, tooling uses `{ops.config_dir}/tls/backup-ca.crt` (e.g. `/etc/indmo/tls/backup-ca.crt`). |

Fixed in-container CA path: `/etc/ssl/backup-ca/ca.crt` (read-only bind from the host CA path).

| Client | Trust |
|--------|--------|
| **restic** | Mount + `AWS_CA_BUNDLE=/etc/ssl/backup-ca/ca.crt` |
| **pgBackRest** | Same mount; managed INI sets `repo1-storage-ca-file=/etc/ssl/backup-ca/ca.crt` when a CA path is active; keep `verify_tls: y` |

Compose gets an ephemeral override (same pattern as the local proxy) that sets `db.extra_hosts` and the CA volume. After `backup-tls issue` / `install`, **recreate `db`** once (`dk <env> up -d --force-recreate db` or equivalent).

## `backup_docker_host` in `info.yaml`

Separate SSH URL for the machine that terminates TLS in front of backup S3 (Garage/Caddy). Same shape as `docker_host` (`ssh://user@host`). Used only by `dk <env> backup-tls install` / status `--check-remote` — not by normal `dk up`.

```yaml
docker_host: ssh://root@172.16.92.27          # app stack
backup_docker_host: ssh://root@172.16.92.28   # backup S3 / Caddy
# env:
#   DOCKER_ADD_HOST: "s3.backup.internal:172.16.92.28"  # only if using a hostname endpoint
#   BACKUP_CA_FILE: /custom/path/ca.crt                 # optional override
```

## SOPS layout

Encrypted file (not folded into `credentials.yaml`):

`docker/envs/<env>/backup-tls.yaml`

```yaml
backup_ca_crt: |
  -----BEGIN CERTIFICATE-----
  …
backup_ca_key: |
  -----BEGIN … PRIVATE KEY-----
  …
backup_server_crt: |
  …
backup_server_key: |
  …
backup_server_ips: ["172.16.92.28"]
backup_server_dns: ["s3.backup.internal"]   # optional
```

Add matching `.sops.yaml` creation rules (same age recipients as `credentials.yaml` for that env), e.g. path_regex `docker/envs/prod/backup-tls\.yaml` and `docker/envs/[^/]+/backup-tls\.yaml`. Exclude the file in `.cursorignore` (see package template).

## CLI

```bash
dk prod backup-tls issue --ip 172.16.92.28 [--dns s3.backup.internal] [--days 825] [--force]
dk prod backup-tls install
dk prod backup-tls status [--check-remote]
```

| Subcommand | Behavior |
|------------|----------|
| `issue` | `openssl` generates a private CA + server cert (SAN: all `--ip` / `--dns`). Writes/updates SOPS `backup-tls.yaml`. Does **not** print private keys. |
| `install` | Decrypt PEMs → CA+server on `backup_docker_host`, CA only on app `docker_host`, under `{ops.config_dir}/tls/` (e.g. `/etc/indmo/tls/`). |
| `status` | File presence, SANs, PEM key presence (no dumps). Optional remote path probes. |

**Caddy/Garage:** install places files only. Point the backup host Caddyfile `tls` at `backup-server.crt` / `backup-server.key` (managing Garage/Caddy compose is out of scope for this tooling command).

## Consumer flow (hostname or IP endpoint)

Prefer a **bare IP** S3 endpoint with an IP SAN (no DC DNS). Example:

```yaml
# credentials (pgBackRest / restic)
pgbr_s3_write_endpoint: "172.16.92.28"
pgbr_s3_write_uri_style: path
pgbr_s3_write_verify_tls: y
restic_write_repository: s3:172.16.92.28/indmo-backups/...
```

1. Set `backup_docker_host` + app `docker_host` in `info.yaml`.
2. `dk <env> backup-tls issue --ip …` → SOPS `backup-tls.yaml` (CA path inferred automatically).
3. `dk <env> backup-tls install` → PEMs on hosts.
4. Configure Caddy TLS paths on the backup host.
5. Recreate `db`; run `db` / `files` backup commands.
