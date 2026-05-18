# catalpa-workspace-tooling

Deploy and development CLIs for Docker-based application stacks. Behavior is driven by a **`tooling.yaml`** manifest at the consumer project root (or via the `TOOLING_CONFIG` environment variable).

## Install

From a consumer repository with [uv](https://docs.astral.sh/uv/):

```bash
uv add "catalpa-workspace-tooling @ git+https://github.com/catalpainternational/catalpa-workspace-tooling@v0.1.0"
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
| `dk` | Docker stack deploy, backup/restore, transfer, Zabbix, etc. |
| `doctl` | DigitalOcean auth and project droplet listing (wraps host `doctl`) |
| `test` | Run backend pytest, frontend Vitest, or repo-root tooling tests |
| `scripts` | Run shell scripts from `paths.scripts` in the manifest |

Run from the **application repo root** (where `tooling.yaml` lives):

```bash
uv run dk --help
```

## Requirements

- Python 3.12+
- Consumer repo must include a valid `tooling.yaml` (see INDMO `data_import` for a reference manifest)
- Host tools for deploy workflows: Docker, `uv`, `sops`, `age` (as needed by your project)
- For DigitalOcean: [doctl](https://docs.digitalocean.com/reference/doctl/) on `PATH` (or set `DOCTL_BIN`); run `doctl auth init` once per machine (prompts for a token). If a stale token is stored, run `doctl auth remove --context default` first, or pass a new token with `doctl auth init -t TOKEN`

### DigitalOcean PAT scopes

Create a [personal access token](https://docs.digitalocean.com/reference/api/create-personal-access-token/) for the team that owns your projects. The `doctl` wrapper in this package calls the DigitalOcean API via host `doctl`; insufficient scopes show up as `403` errors from `doctl`.

| What you use | Scopes |
|--------------|--------|
| `doctl projects list`, `doctl droplets list` | `project:read`, `droplet:read` |
| `doctl droplets create` (and default SSH keys from the account) | above, plus `droplet:create`, `ssh_key:read` |

`droplet:read` and `droplet:create` require companion read scopes (`regions:read`, `sizes:read`, `actions:read`, `image:read`); the [custom scopes picker](https://cloud.digitalocean.com/account/api/tokens) adds these when you select droplet scopes. See [Scopes for API tokens](https://docs.digitalocean.com/reference/api/scopes/) for the full list.

Convenience aliases (token UI: **Read** / **Full Access**):

- **Read only** — `api:read` — enough for listing projects and droplets.
- **Full access** — `api:write` — covers droplet create and SSH key listing without picking granular scopes.

`doctl cloud-config print` does not call the API (no token needed).

Optional `tooling.yaml` block for DigitalOcean defaults:

```yaml
digitalocean:
  project_name: my-do-project
  context: default   # optional doctl auth context
  timezone: Asia/Dili
  region: sgp1
  size: s-2vcpu-4gb
  image: ubuntu-24-04-x64
  ssh_keys:
    - "aa:bb:cc:..."   # fingerprint or ID from `doctl compute ssh-key list`
```

Bootstrap a new droplet (Docker CE, UFW, unattended upgrades, SSH key-only):

```bash
doctl cloud-config print --timezone Asia/Dili
doctl droplets create my-host --project my-do-project --dry-run
doctl droplets create my-host --wait   # uses digitalocean.* from tooling.yaml
```

By default, **all SSH keys** on your DigitalOcean account are embedded (`doctl compute ssh-key list`). Override with `--ssh-key` (repeatable) or `digitalocean.ssh_keys` in `tooling.yaml`.

## Documentation

Full onboarding and manifest reference are planned (`ONBOARDING.md`, `CONFIG_REFERENCE.md`). Until then, use an existing consumer’s `tooling.yaml` and deploy docs as a template.

## Development

```bash
uv sync --group test
uv run pytest
uv build
```
