# Troubleshooting catalpa-workspace-tooling

Concise checklist for consumer apps that use `dk` / `native` / related CLIs. Run commands from the **application repo root** (where `tooling.yaml` lives).

Topic-specific tables also live in [README_PGBACKREST.md](README_PGBACKREST.md#troubleshooting), [README_RESTIC.md](README_RESTIC.md), [README_DC_BACKUP.md](README_DC_BACKUP.md), [README_SYSTEMD.md](README_SYSTEMD.md), and [ZABBIX_README.md](ZABBIX_README.md).

---

## 1. Config map (what lives where)

| Layer                   | Typical paths                                   | Holds                                                           | Notes                                                              |
| ----------------------- | ----------------------------------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------ |
| Manifest                | `tooling.yaml`                                  | Project name, compose defaults, service **roles**, image names. | `ops.*`: pgBackRest paths, volume keys, systemd, install prefixes. |
| SOPS policy             | `.sops.yaml`                                    | **Who can decrypt** credential files (age/PGP rules).           | Not runtime env values.                                            |
| Deploy env (non-secret) | `docker/envs/<env>/info.yaml`                   | `docker_host`, `compose_file`, `image_tag`, `site_origin`, …    | Plus `env:` (e.g. `compose_project_name`, `PROJECT_NAME`).         |
| Deploy secrets          | `docker/envs/<env>/credentials.yaml`            | SOPS-encrypted secrets and backup keys.                         | e.g. `postgres_password`, `pgbr_s3_write_*`, `restic_write_*`.     |
| Examples only           | `credentials.example.yaml`                      | Structure for humans; not loaded by `dk`.                       |                                                                    |
| Images                  | `docker/images.yaml` (path from `tooling.yaml`) | Registry / tag defaults when `info.yaml` omits them.            |                                                                    |
| Compose / images        | `compose.yaml`, includes, Dockerfiles           | Service definitions, volume **keys**, mount paths.              | `name:` / `${PROJECT_NAME}` / `${COMPOSE_PROJECT_NAME}`.           |
| Host / native           | `.env.local` (see `paths.env_local`)            | Local Django / `native` on the host.                            | **Not** the same merge as `dk <env>`.                              |
| On the droplet          | `{ops.config_dir}/*.env`, systemd units         | e.g. `PGBR_DB_CONTAINER` after `db install-systemd`.            | Separate from repo YAML until you reinstall.                       |

`info.yaml` `env:` keys and credential keys are uppercased when loaded (`compose_project_name` → `COMPOSE_PROJECT_NAME`). Nested dicts/lists in credentials are skipped.

---

## 2. Precedence (same variable, several places)

When diagnosing “where did this value come from?”, use this order for a typical `dk <env> …` run:

1. **CLI flags** that the command documents (e.g. `--tag` over `info.yaml` `image_tag`).
2. **Decrypted `credentials.yaml`** — overwrites the same key from `info.yaml` `env:`.
3. **`info.yaml` `env:`** — non-secret deploy env.
4. **Derived / injected by tooling** — e.g. `DOCKER_HOST` from top-level `docker_host`; `SITE_ORIGIN` / `DOMAIN` from `site_origin`; `COMPOSE_PROJECT_NAME` if still unset (`stack.compose_project_default`, plus `_<env>` for local engines).
5. **`tooling.yaml` defaults** — e.g. `ops.pgbackrest.pg1_path`, image component names, volume keys.
6. **Compose file / image defaults** — `${VAR:-fallback}` inside YAML; entrypoint / Dockerfile `ENV`.

**Stale copies:** materializing pgBackRest/Postgres drop-ins (`dk <env> db configure`) and installing systemd env files write **onto the remote host**. Editing credentials alone does not refresh those until you re-run configure / `install-systemd`.

**Do not confuse:**

| Name                             | Used by                                                    | Example                                            |
| -------------------------------- | ---------------------------------------------------------- | -------------------------------------------------- |
| Compose **service**              | `docker compose … db`, `dk` backup helpers                 | `db`                                               |
| Docker **container**             | `docker exec`, systemd `PGBR_DB_CONTAINER`, `native.fetch` | `ncd-db-1`                                         |
| Compose **project**              | Names containers/volumes                                   | `ncd` → `ncd-db-1`, `ncd_postgres_data`            |
| `project.name` in `tooling.yaml` | Droplet naming, some defaults                              | Often same slug as compose project, not guaranteed |

Messages that say ``starting `db``` mean the **service**. A failure there is usually project/env/PGDATA, not “wrong container string”.

---

## 3. Newcomer checklist

Work top to bottom; stop when the symptom is explained.

### A. Am I in the right repo and env?

- [ ] `cwd` is the app root that contains `tooling.yaml` (or `TOOLING_CONFIG` points at it).
- [ ] `dk` is this project’s venv (`whence dk` / `uv run dk`).
- [ ] Target env folder exists: `docker/envs/<env>/info.yaml`.
- [ ] For staging/prod: `info.yaml` has `docker_host: ssh://…`. Local `dev`/`full` usually omit it.

### B. Can secrets load?

- [ ] `.sops.yaml` covers `docker/envs/.*/credentials.yaml` (and `dc-backup*` if used).
- [ ] Age/PGP key available locally: `sops -d docker/envs/<env>/credentials.yaml >/dev/null && echo OK` (do **not** print decrypted output into chat/logs).
- [ ] If decrypt fails and `credentials_decrypt_optional` is set, `dk` continues **without** secret keys — backups will fail later for a misleading reason.

### C. Compose project and remote Docker

- [ ] `info.yaml` `env.compose_project_name` (→ `COMPOSE_PROJECT_NAME`) matches the stack on the host (`docker ps` names like `{project}-db-1`).
- [ ] If compose uses `name: ${PROJECT_NAME}`, set **`PROJECT_NAME`** in `info.yaml` `env:` as well (same value). Mismatch → wrong project / empty stack / “service not running”.
- [ ] Volume names in compose (`${COMPOSE_PROJECT_NAME}_postgres_data`) match volumes on the host (`docker volume ls`).
- [ ] First remote use: host key in `known_hosts` (`dk <env> host` / `host --write`).

Quick peek (non-secret):

```bash
uv run dk <env> ps
# stderr should show DOCKER_HOST=… and registry/tag when configured
```

### D. Service roles vs your compose file

- [ ] `tooling.yaml` `stack.services.db` / `web` / `proxy` match **service names** in compose (usually `db`, `django`/`web`, `caddy`).
- [ ] Image components under `stack.images.components` match image name suffixes used with the registry.

### E. Postgres / pgBackRest layout (frequent footgun)

- [ ] Compose mounts the data volume where the image expects it (often `postgres_data:/var/lib/postgresql` for PG 18+).
- [ ] `ops.pgbackrest.pg1_path` (or env `PGBR_PG1_PATH`) is the real cluster directory **inside** the container.
  - Official Postgres **18+** images: typically `/var/lib/postgresql/18/docker` (tooling default).
  - Older `/var/lib/postgresql/data` is wrong for those images — `db init` / stanza-create then report **PGDATA missing** even while `db` is healthy.
- [ ] Confirm on a running container:

```bash
docker exec <project>-db-1 bash -c 'echo PGDATA=$PGDATA; find /var/lib/postgresql -name pg_control 2>/dev/null'
docker exec <project>-db-1 grep '^pg1-path=' /etc/pgbackrest/conf.d/*.conf
```

- [ ] After fixing `pg1_path`, re-run `dk <env> db configure` so the managed conf volume picks it up.
- [ ] The **image** `/etc/pgbackrest.conf` (or `/etc/pgbackrest/pgbackrest.conf`) must **not** set `pg1-path`. Tooling writes `pg1-path` (and S3 `repo1-*`) into `/etc/pgbackrest/conf.d/{ops.pgbackrest.pgbackrest_conf}`. The same option in both files → `ERROR: [031]: option 'pg1-path' cannot be set multiple times`. Image `[global]` is only lock/log/spool paths the `postgres` user can write (see [QUICKSTART.md](QUICKSTART.md#postgres-image-and-pgbackrestconf)).

### F. Backup credentials vs host env files

- [ ] WRITE backups: complete `pgbr_s3_write_*` in credentials (not mixed with `pgbr_s3_read_*` in the same env).
- [ ] Systemd timers need a **container name**: set `pgbr_db_container` or let `install-systemd` discover; `ops.default_db_container` is only a dry-run / weak fallback — keep it accurate per env or prefer discovery.
- [ ] On the host, `{ops.config_dir}/pgbackrest-backup.env` should show `PGBR_DB_CONTAINER=<actual-name>` (e.g. `ncd-db-1`), not the service name `db`.

### G. Storage binds

- [ ] If `info.yaml` sets `storage.volumes.<key>.path`, that path must exist and be writable on the **deploy** host before `up` / restore.
- [ ] Bind path and compose volume **key** (`django_media`, `postgres_data`, …) must match `tooling.yaml` / compose.

### H. Seeding a new Docker host from Ansible / native backups

`dk <env> db restore` and `dk <env> files restore` replay **this stack’s** pgBackRest and restic layouts. Do **not** use them to copy data from an Ansible or host-native deploy. After the stack is writing its own backups, those two commands are the right DR path.

**Database.** `dk <env> db restore` is offline pgBackRest whenever `pgbr_s3_read_*` or `pgbr_s3_write_*` are complete. Debian/Ubuntu package clusters (Ansible-era hosts) keep `postgresql.conf` / `pg_hba.conf` under `/etc/postgresql/…`, outside PGDATA. After a physical restore, the official image skips `initdb` (“directory appears to contain a database”) and then `FATAL: could not load $PGDATA/pg_hba.conf`.

For those sources: `pg_dump -Fc` on the old host, place the archive at `paths.fetch_db_dump` (and Metabase at `paths.fetch_metabase_db_dump` if used), then:

```bash
uv run dk <env> db pgrestore --file docker/dumps/app.custom   # app DB only
# or both configured dumps (never pgBackRest; never auto-fetch):
uv run dk <env> db restore --dumps
```

`db` must already be a **healthy Docker-initialized** volume (wipe/recreate `postgres_data` if a Debian pgBackRest restore already landed). See [README_PGBACKREST.md](README_PGBACKREST.md#bkp_db-subcommands).

**Media.** Ansible restic snapshots store host paths (e.g. `/var/www/app/public/media`). `files restore` filters `--path /backup/<volume>` (or `ops.restic.backup_path`), so those snapshots miss, extract under the old prefix, and need a second full-size staging volume — a metadata failure then discards the extract. Copy the live tree instead:

```bash
uv run dk fetch media            # legacy host path when native.fetch_media.legacy is set
uv run dk <env> files push
```

See [README_RESTIC.md](README_RESTIC.md#ansible--host-path-snapshots).

---

## 4. Symptom → first places to look

| Symptom                                                 | Check first                                                                            |
| ------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `Missing …/credentials.yaml` or `sops decrypt failed`   | File present? `.sops.yaml` rules? Local age key?                                       |
| Wrong or empty stack on remote                          | `DOCKER_HOST`, `COMPOSE_PROJECT_NAME` / `PROJECT_NAME`, `compose_file`                 |
| ``service `db` is not running`` / ``starting `db```     | Service name is expected; fix project env or `dk <env> up -d db`                       |
| `No such container: db`                                 | Something used a **container** name; should be `{project}-db-1` or `PGBR_DB_CONTAINER` |
| `PGDATA still missing after starting \`db\``            | `ops.pgbackrest.pg1_path` vs real `PGDATA` (see §3.E)                                  |
| `[031]: option 'pg1-path' cannot be set multiple times` | Image `pgbackrest.conf` also sets `pg1-path` (see §3.E)                                |
| `could not load …/pg_hba.conf` after `db restore`       | pgBackRest from a Debian/non-Docker source (see §3.H); use dumps                       |
| `dk <env> db restore` ran pgBackRest instead of a dump  | Complete `pgbr_s3_*` keys; pass `--dumps` or use `db pgrestore`                        |
| `no snapshot found` / `lchown` on `files restore`       | Ansible/host-path restic snapshot (see §3.H); use `dk fetch media` + `files push`      |
| `Could not discover a unique db container`              | Stack up? One Postgres? Set `pgbr_db_container`                                        |
| Stale S3 path / stanza after editing credentials        | Re-run `dk <env> db configure`; compare with `db restore --dry-run`                    |
| Image pull / wrong tag                                  | `info.yaml` `image_tag`, `--tag`, `STACK_IMAGE_REGISTRY`, `docker/images.yaml`         |
| Local proxy / `*.localdev.temp.build` issues            | [README_DEV_PROXY.md](README_DEV_PROXY.md); `compose_project_name` for that env        |
| Native host DB vs Docker DB confusion                   | `.env.local` for `native`; `dk <env>` for compose — different configs                  |
| `host create` finished but SSH / DNS / lookup fails     | [§ Host create and DNS](#host-create-and-dns)                                          |
| 403 from `doctl` / droplet in the wrong DO project      | [README.md](README.md#digitalocean-pat-scopes)                                         |

---

## 5. Safe inspection habits

- Prefer `credentials.example.yaml` and `info.yaml` for structure.
- Decrypt only to verify: `sops -d … >/dev/null`.
- Avoid pasting `docker compose config` / decrypted YAML into tickets or AI chats.
- Global `dk --dry-run` does **not** make every subcommand a no-op (notably some `db init` / volume paths still touch the remote). Read the command’s stderr before assuming dry-run is safe.

Agent setup for secrets and remote confirmation: [docs/AGENTS_AND_SECRETS.md](docs/AGENTS_AND_SECRETS.md).

---

## 6. Host create and DNS

First-time provision and deploy: [QUICKSTART.md](QUICKSTART.md#7-first-remote-environment-digitalocean-droplet-and-deploy). Use this section when `dk <env> host create` only partly succeeded.

After `host create` or `host --write`, tooling registers the deploy host’s SSH key in `~/.ssh/known_hosts` (`ssh-keyscan`, with retries until sshd answers) so later `dk <env> …` can use `DOCKER_HOST=ssh://…` without a manual first login. The same check runs before other remote `dk` commands when `docker_host` is SSH-formatted, and before `native fetch db` / `native fetch media` when they SSH to a configured host.

**New droplets:** DigitalOcean may report `active` before SSH accepts connections on port 22. The tooling waits up to ~2 minutes; if registration still fails, `docker_host` is usually already patched — finish with `dk <env> host --write` (or re-run `host create`, which reuses the existing droplet by default).

| Step                                          | Command                                    | Notes                                                                           |
| --------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------- |
| SSH host key                                  | `dk <env> host --write`                    |                                                                                 |
| DO A records only                             | `dk <env> host --sync-dns`                 |                                                                                 |
| Verify DNS                                    | `dk <env> host`                            |                                                                                 |
| Resume all post-create steps                  | `dk <env> host create`                     | Reuses existing droplet by default.                                             |
| Droplet in default DO project (e.g. Internal) | Fix token (`project:update`), then create. | Or `doctl projects resources assign <project-uuid> --resource=do:droplet:<id>`. |

**What `dk <env> host` checks (DigitalOcean):** with doctl, the droplet exists, status is `active`, and public IPv4 is available. Lookup is scoped to `digitalocean.project_name` / `project_id` in `tooling.yaml` when set. When `site_origin` and/or `redirect_origins` are set, it verifies (1) DigitalOcean DNS API — A records (or CNAMEs that chain to an apex A) on DO-managed zones must point at the droplet IP; zones must be in the project; hostnames not on DO DNS are skipped with a warning — and (2) **public DNS** via the system resolver (Python stdlib, no `dig`): each hostname must resolve to that IP. `host create` syncs DO A records then runs both checks. `host --write` does not sync or verify DNS.

**Non-DO or manual host:** `digitalocean.disabled: true` in `info.yaml`; maintain `docker_host` (+ optional `dc_backup_docker_host`) and `site_origin`. `dk <env> host` skips droplet lookup and the DO DNS API; it prints configured hosts and checks public DNS only. `host create` and `host --write` are not available.

If doctl is available but no matching droplet exists and `docker_host` is already set, `dk <env> host` uses the same manual status path (no `host create` hint) and notes that `digitalocean.disabled: true` opts out of doctl permanently. Without doctl but with `docker_host` set, behavior matches the disabled path.

Optional `--check-remote` SSHes to `docker_host` and `dc_backup_docker_host` (when set): BatchMode reachability plus `timedatectl` (timezone and NTP). Timezone mismatch between app and backup hosts, or NTP not synchronized, prints a soft warning; exit stays 0 unless SSH itself fails hard.

**Caveats:** public DNS uses the machine’s resolver (VPN, `/etc/hosts`, caching). A CDN or proxy in front of the origin can make the public check fail while DO API records are correct. Token 403s: [README.md](README.md#digitalocean-pat-scopes).

---

## 7. Further reading

| Guide                                                                                      | When                                               |
| ------------------------------------------------------------------------------------------ | -------------------------------------------------- |
| [QUICKSTART.md](QUICKSTART.md)                                                             | First local run, droplet create, first deploy      |
| [README.md](README.md)                                                                     | Completion, DO/GHCR scopes, `site_origin`, storage |
| [README_PGBACKREST.md](README_PGBACKREST.md)                                               | `db` / `bkp_db`, S3 keys, `pg1_path`, stanza       |
| [README_RESTIC.md](README_RESTIC.md)                                                       | Media file backups                                 |
| [README_SYSTEMD.md](README_SYSTEMD.md)                                                     | Timers and host env files                          |
| [README_DC_BACKUP.md](README_DC_BACKUP.md)                                                 | Private Garage / CA / `DOCKER_ADD_HOST`            |
| [docs/DK_COMMAND_TREE.md](docs/DK_COMMAND_TREE.md)                                         | Command map                                        |
| [tests/fixtures/indmo_reference_tooling.yaml](tests/fixtures/indmo_reference_tooling.yaml) | Full `tooling.yaml` example                        |
