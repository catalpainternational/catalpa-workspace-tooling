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
| `dev` | Local development helpers (backend, frontend, prototype) |
| `dk` | Docker stack deploy, backup/restore, transfer, Zabbix, DigitalOcean (`dk digoc`), etc. See [Backup and monitoring](#backup-and-monitoring). |
| `test` | Run backend pytest, frontend Vitest, or repo-root tooling tests |
| `scripts` | Run shell scripts from `paths.scripts` in the manifest |

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

Create a [personal access token](https://docs.digitalocean.com/reference/api/create-personal-access-token/) for the team that owns your projects. `dk digoc` calls the DigitalOcean API via the host `doctl` binary; insufficient scopes show up as `403` errors.

| What you use | Scopes |
|--------------|--------|
| `dk digoc projects list`, `dk digoc droplets list` | `project:read`, `droplet:read` |
| `dk digoc droplets create` (and default SSH keys from the account) | above, plus `droplet:create`, `ssh_key:read` |

`droplet:read` and `droplet:create` require companion read scopes (`regions:read`, `sizes:read`, `actions:read`, `image:read`); the [custom scopes picker](https://cloud.digitalocean.com/account/api/tokens) adds these when you select droplet scopes. See [Scopes for API tokens](https://docs.digitalocean.com/reference/api/scopes/) for the full list.

Convenience aliases (token UI: **Read** / **Full Access**):

- **Read only** — `api:read` — enough for listing projects and droplets.
- **Full access** — `api:write` — covers droplet create and SSH key listing without picking granular scopes.

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
```

Bootstrap a new droplet (Docker CE, UFW, unattended upgrades, SSH key-only):

```bash
dk digoc cloud-config print --timezone Asia/Dili
dk digoc droplets create my-host --project my-do-project --dry-run
dk digoc droplets create my-host --wait   # uses digitalocean.* from tooling.yaml
```

By default, **all SSH keys** on your DigitalOcean account are embedded (via host `doctl compute ssh-key list`). Override with `--ssh-key` (repeatable) or `digitalocean.ssh_keys` in `tooling.yaml`.

### Linking droplets to `dk` environments

In each remote env’s `docker/envs/<env>/info.yaml`, set:

```yaml
digitalocean:
  droplet_name: my-hostname
  ssh_user: root   # optional
```

After creating the droplet:

```bash
dk digoc droplets create --for-env prod --wait
dk prod host              # print suggested docker_host
dk prod host --write      # patch info.yaml
dk digoc droplets list    # includes Env column when tooling.yaml is present
dk digoc droplets suggest-env prod   # same as dk prod host
```

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
