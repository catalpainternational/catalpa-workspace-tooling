# catalpa-workspace-tooling

Deploy and development CLIs for Docker-based application stacks. Behavior is driven by a **`tooling.yaml`** manifest at the consumer project root (or via the `TOOLING_CONFIG` environment variable).

## Install

From a consumer repository with [uv](https://docs.astral.sh/uv/):

```bash
uv add "catalpa-workspace-tooling @ git+https://github.com/catalpainternational/catalpa-workspace-tooling@v0.1.3"
```

For local development of this library:

```bash
uv add --editable ../catalpa-workspace-tooling
```

## Commands

After install, these console scripts are available:

| Command | Purpose |
|---------|---------|
| `dev` | Local development helpers (Django, Vite, fetch, plus `scripts/dev-*.sh` extensions) |
| `dk` | Docker stack deploy, backup/restore, transfer, Zabbix, DigitalOcean (`dk digoc`), etc. See [Backup and monitoring](#backup-and-monitoring). On macOS, `dk <env> trust-caddy-cert` trusts Caddy's local HTTPS CA for that env's compose stack. |
| `test` | Run backend pytest, frontend Vitest, or repo-root tooling tests |
| `scripts` | Run `scripts/*.sh` helpers (auto-discovered; excludes `dev-*.sh`) |

### Project script extensions

Place bash scripts under `paths.scripts` in `tooling.yaml`:

- **`scripts/dev-<name>.sh`** → `uv run dev <name>` (e.g. `dev-storybook.sh` → `dev storybook`). Built-in `dev` commands (`runserver`, `vite`, `reset-db`, …) take precedence over discovered names.
- **`scripts/<name>.sh`** (not `dev-*`) → `uv run scripts <kebab-name>` (e.g. `fetch_db.sh` → `scripts fetch-db`).
- **`scripts/dev-reset-db-post.sh`** — optional hook after `dev reset-db` recreates the DB and enables PostGIS; replaces the default `migrate`-only tail step.

For npm-based dev servers, source the bundled helper:

```bash
# shellcheck source=/dev/null
source "$(uv run python -c 'from catalpa_tooling.script_assets import npm_run_helper_path; print(npm_run_helper_path())')"
npm_run_in_dir "${REPO_ROOT}/frontend_vue" storybook
```

Run from the **application repo root** (where `tooling.yaml` lives):

```bash
uv run dk --help
uv run dk digoc --help
```

## Requirements

- Python 3.12+
- Consumer repo must include a valid `tooling.yaml` (see INDMO `data_import` for a reference manifest)
- Host tools for deploy workflows: Docker, `uv`, `sops`, `age` (as needed by your project)
- For DigitalOcean: install the official [doctl](https://docs.digitalocean.com/reference/doctl/) on `PATH` (or set `DOCTL_BIN`). Use **`dk digoc`** for project wrappers (auth, droplets, cloud-config). Run `dk digoc auth init` once per machine. If a stale token is stored, run `dk digoc auth remove --context default` first, or pass a new token with `dk digoc auth init -t TOKEN`

### DigitalOcean PAT scopes

Create a [personal access token](https://docs.digitalocean.com/reference/api/create-personal-access-token/) for the team that owns your projects. `dk` and `dk digoc` call the DigitalOcean API via the host `doctl` binary; insufficient scopes show up as `403` errors.

| What you use | Scopes |
|--------------|--------|
| `dk digoc projects list`, project resolution | `project:read` |
| `dk digoc droplets list`, `dk <env> host` (droplet verify) | `project:read`, `droplet:read` — project-scoped droplet lookup also calls `projects resources list` |
| `dk digoc droplets create`, `dk <env> host create` | above, plus `droplet:create`, **`ssh_key:read`** (lists keys via `GET /v2/account/keys` — not `account:read`) |
| `dk <env> host` (DNS verify for `site_origin`) | above, plus `domain:read` |
| `dk <env> host create` (DNS sync after droplet create) | above, plus `domain:write` (or granular domain record create/update) |
| `dk <env> bkp_db` / `bkp_files` auto-provision (missing WRITE creds) | `spaces_key:read`, `spaces_key:create_credentials`; bootstrap may call `spaces keys delete` → `spaces_key:delete` |

`droplet:read`, `droplet:create`, and domain/spaces scopes require companion read scopes (`regions:read`, `sizes:read`, `actions:read`, `image:read`, etc.); the [custom scopes picker](https://cloud.digitalocean.com/account/api/tokens) adds these when you select those scopes. See [Scopes for API tokens](https://docs.digitalocean.com/reference/api/scopes/) for the full list.

**403 on `doctl compute ssh-key list`:** the token is missing **`ssh_key:read`**. That call uses the account keys API (`/v2/account/keys`); [`account:read`](https://docs.digitalocean.com/reference/api/scopes/account/read) is only for profile/billing-style account metadata, not SSH keys. Fix: add `ssh_key:read` to the token, use **Full Access** (`api:write`), or avoid listing by passing explicit keys: `dk <env> host create --ssh-key ID` (repeatable) or `digitalocean.ssh_keys` in `tooling.yaml` (IDs/fingerprints from the control panel or a token that can list keys once).

Convenience aliases (token UI: **Read** / **Full Access**):

- **Read only** — `api:read` — listing projects, droplets, domains, and Spaces keys.
- **Full access** — `api:write` — droplet create, DNS sync, Spaces key create, and SSH key listing without picking granular scopes.

**Host tools (not PAT):** Spaces bucket create/check uses host `s3cmd` (`mb`, `info`); credential updates use `sops` — see [README_PGBACKREST.md](README_PGBACKREST.md#auto-provision-digitalocean-spaces).

`dk digoc cloud-config print` does not call the API (no token needed).

Optional `tooling.yaml` block for DigitalOcean defaults:

```yaml
digitalocean:
  project_name: my-do-project
  context: default   # optional host doctl auth context
  timezone: Asia/Dili
  region: sgp1
  size: s-2vcpu-4gb
  image: ubuntu-24-04-x64
  ssh_keys:
    - "aa:bb:cc:..."   # fingerprint or ID from host `doctl compute ssh-key list`
  monitoring: true      # optional; default true — passes --enable-monitoring to doctl
```

Bootstrap a new droplet (Docker CE, UFW, unattended upgrades, SSH key-only). By default the DigitalOcean metrics agent (`do-agent`) is enabled via `--enable-monitoring` ([DO docs](https://docs.digitalocean.com/products/monitoring/how-to/install-metrics-agent/)); pass `--no-monitoring` or set `digitalocean.monitoring: false` to skip.

```bash
dk digoc cloud-config print --timezone Asia/Dili
dk digoc droplets create my-host --project my-do-project --dry-run
dk digoc droplets create my-host --wait   # uses digitalocean.* from tooling.yaml
```

By default, **all SSH keys** on your DigitalOcean account are embedded (via host `doctl compute ssh-key list`). Override with `--ssh-key` (repeatable) or `digitalocean.ssh_keys` in `tooling.yaml`. DO Insights metrics use `--enable-monitoring` by default (complements Zabbix; see [ZABBIX_README.md](ZABBIX_README.md)).

### Linking droplets to `dk` environments

By default the DigitalOcean droplet name is **`{project.name}-{env}`** from `tooling.yaml` and the deploy env folder (e.g. `catalpa-site-prod`). Override in `docker/envs/<env>/info.yaml`:

```yaml
digitalocean:
  droplet_name: my-hostname   # optional
  ssh_user: root              # optional
  disabled: false             # true: manual docker_host only (no droplet / DO DNS API)
  size: s-2vcpu-4gb           # optional; used by `dk <env> host create`
  region: sgp1                # optional; used by `dk <env> host create`
  dns_ttl: 3600               # optional; DO A record TTL in seconds (default 300; e.g. 3600 for prod)
```

For `host create`, resolution order is **CLI flag → env `info.yaml` → `tooling.yaml`** for `size` and `region`.

Provision and link a new droplet:

```bash
dk prod host create       # create droplet, wait, patch docker_host, sync DNS A records on DO zones
dk prod host              # verify droplet + site_origin DNS (DO API + public resolution)
dk prod host --write      # refresh docker_host from droplet public IPv4
dk digoc droplets list    # includes Env column when tooling.yaml is present
```

After `host create` or `host --write`, the tooling registers the deploy host’s SSH key in your `~/.ssh/known_hosts` (via `ssh-keyscan`) so the next `dk <env> …` command can use `DOCKER_HOST=ssh://…` without a manual first `ssh` login. The same check runs idempotently before other remote `dk` commands when `docker_host` is SSH-formatted.

**Default (DigitalOcean):** With doctl, `dk <env> host` checks the droplet exists, status is `active`, and public IPv4 is available; lookup is scoped to `digitalocean.project_name` / `project_id` in `tooling.yaml` when set. When `site_origin` is set, it verifies (1) DigitalOcean DNS API — A records on DO-managed zones must point at the droplet IP, zones must be in the project; hostnames not on DO DNS are skipped with a warning — and (2) **public DNS** via the system resolver (Python stdlib, no `dig` required): each `site_origin` hostname must resolve to that IP. `dk <env> host create` creates or updates DO A records after the droplet is active, then runs both checks (not on `host --write`).

**Non-DO or manual host:** Set `digitalocean.disabled: true` in `docker/envs/<env>/info.yaml` and maintain `docker_host` + `site_origin`. `dk <env> host` skips droplet lookup and DO API DNS; it checks public DNS only. `host create` and `host --write` are not available in this mode. Without doctl but with `docker_host` set, behavior matches the disabled path (public DNS when `site_origin` is set).

**Caveats:** Public DNS uses the machine’s resolver (VPN, `/etc/hosts`, caching). A CDN or proxy in front of the origin can make the public check fail while DO API records are correct.

### Backup and monitoring

Topic guides (run `dk <env> …` from the application repo root):

| Topic | Guide |
|-------|--------|
| pgBackRest (S3, `bkp_db`, WAL archive) | [README_PGBACKREST.md](README_PGBACKREST.md) |
| Restic (media files, `bkp_files`) | [README_RESTIC.md](README_RESTIC.md) |
| Systemd timers on deploy hosts | [README_SYSTEMD.md](README_SYSTEMD.md) |
| Zabbix Agent 2 | [ZABBIX_README.md](ZABBIX_README.md) |

### `site_origin` in `info.yaml`

Each deploy environment’s `docker/envs/<env>/info.yaml` may set **`site_origin`** as a hostname, full URL, or YAML list of either. The `dk` CLI derives:

| Compose env | Value |
|-------------|--------|
| `SITE_ORIGIN` | First normalized origin (e.g. `https://catalpa.io`) |
| `DOMAIN` | Comma+space joined hostnames for Caddy and Django (e.g. `catalpa.io, www.catalpa.io`) |

Top-level **`domain`** (string or list) is still accepted but deprecated; prefer `site_origin`. Nested `env.site_origin` / `env.domain` are used only when the top-level field is empty.

## Documentation

| Document | Contents |
|----------|----------|
| [README_PGBACKREST.md](README_PGBACKREST.md) | `pgbr_s3_*` credentials, volume materialize, `bkp_db` |
| [README_RESTIC.md](README_RESTIC.md) | `restic_*` credentials, `bkp_files` |
| [README_SYSTEMD.md](README_SYSTEMD.md) | `ops.systemd_units`, `install-systemd` on deploy hosts |
| [ZABBIX_README.md](ZABBIX_README.md) | `dk <env> zabbix`, UserParameters |

Full onboarding and manifest reference are planned (`ONBOARDING.md`, `CONFIG_REFERENCE.md`). Until then, use an existing consumer’s `tooling.yaml` (e.g. INDMO `data_import`) and that project’s `docker/envs/` layout as a template.

## Development

```bash
uv sync --group test
uv run pytest
uv build
```
