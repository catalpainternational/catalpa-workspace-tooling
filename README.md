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

### Shell completion (optional)

Install the completion extra in each consumer repo, then wire shell completion once globally and per-repo via direnv.

**1. Per-repo dependency (each tooling project):**

```bash
uv add --group tooling 'catalpa-workspace-tooling[completion]'
uv sync
```

**2. Per-repo direnv (each tooling project):**

Copy [`scripts/envrc.template`](scripts/envrc.template) to `.envrc`, pick minimal or recommended variant, then:

```bash
direnv allow
```

The `.envrc` puts `.venv/bin` on `PATH` (so `dk`, `dev`, etc. resolve to that repo's venv) and exports `CATALPA_REGISTER_PYTHON_ARGCOMPLETE` for the zsh hook below. Each repo can pin a different tooling version; switching directories switches which `dk` binary completion invokes.

**3. One-time machine setup (zsh, any Catalpa tooling repo):**

Run once after `uv sync` (before `direnv allow` — `dk` is not on PATH yet):

```bash
uv run setup-shell
```

This installs `~/.config/catalpa/direnv.zsh` from the package and patches `~/.zshrc` with the direnv hook plus the completion source. Re-run safely; use `uv run setup-shell --status` to check. Remove with `uv run setup-shell --remove`.

Open a new shell, `cd` into a tooling repo, and `direnv allow`. Verify: `whence dk` → `…/.venv/bin/dk`.

**VS Code tasks (optional, per tooling repo):**

After `uv sync`, scaffold dev tasks once per project:

```bash
uv run setup-vscode
```

Then use **Terminal → Run Task** (Cmd+Shift+P → “Tasks: Run Task”). Django compose projects get **dk dev** tasks (start/stop stack, logs, open site, Django manage, backup restore) and **dk full** tasks when `docker/envs/full/info.yaml` exists. SSH-backed fetch tasks are not included — run `native fetch db` / `native fetch media` from a terminal when needed.

Check status with `uv run setup-vscode --status`. Remove scaffolded files with `uv run setup-vscode --remove`.

**Manual fallback (no `setup-shell`):** copy [`scripts/catalpa-direnv.zsh`](scripts/catalpa-direnv.zsh) to `~/.config/catalpa/direnv.zsh`, add to `~/.zshrc` **after** `compinit` and `eval "$(direnv hook zsh)"`:

```zsh
eval "$(direnv hook zsh)"
[[ -f "${XDG_CONFIG_HOME:-$HOME/.config}/catalpa/direnv.zsh" ]] && \
  source "${XDG_CONFIG_HOME:-$HOME/.config}/catalpa/direnv.zsh"
```

Remove any global `dk() { uv run dk "$@"; }` or global `eval "$(register-python-argcomplete …)"` lines — direnv + the hook replace them.

**4. Cursor agents (recommended for repos with SOPS credentials):**

Copy [`scripts/cursorignore.template`](scripts/cursorignore.template) to `.cursorignore` and both [`scripts/cursor-rules/secrets-and-agents.mdc`](scripts/cursor-rules/secrets-and-agents.mdc) and [`scripts/cursor-rules/remote-environments.mdc`](scripts/cursor-rules/remote-environments.mdc) to `.cursor/rules/`. For Django compose consumers with Playwright smoke tests, optionally add [`scripts/cursor-rules/smoke-tests.mdc`](scripts/cursor-rules/smoke-tests.mdc). See [docs/AGENTS_AND_SECRETS.md](docs/AGENTS_AND_SECRETS.md).

**Single-project / bash (no direnv):**

```bash
export PATH="$PWD/.venv/bin:$PATH"
eval "$(uv run register-python-argcomplete -s zsh dk)"
eval "$(uv run register-python-argcomplete -s zsh native)"
eval "$(uv run register-python-argcomplete -s zsh local)"
eval "$(uv run register-python-argcomplete -s zsh dev)"
eval "$(uv run register-python-argcomplete -s zsh test)"
eval "$(uv run register-python-argcomplete -s zsh scripts)"
```

Or run [`scripts/install-completions.sh`](scripts/install-completions.sh) in a shell where the project venv is active.

**Verify completion**

After `cd` into a tooling repo (with direnv loaded):

```zsh
whence dk                         # → …/.venv/bin/dk
echo "${_comps[dk]:-NOT REGISTERED}"   # → _python_argcomplete
```

CLI probe (argcomplete writes to fd 8, not stdout):

```bash
_ARGCOMPLETE=1 COMP_LINE="dk full i" COMP_POINT=9 \
  _ARGCOMPLETE_STDOUT_FILENAME=/tmp/dk-comp dk && tr '\013' ' ' </tmp/dk-comp
# expect: … info secrets host …
```

Notes:

- Do **not** use `compdef -p` to check registration — in zsh, `-p` means pattern mode, not print.
- `_python_argcomplete` may already exist from gcloud; check `_comps[dk]` instead.
- `watch_file` in `.envrc` only helps when paired with `uv sync` on reload; PATH_add alone does not need it.
- For docker compose operations, use `dk <env> compose up -d` for tab completion; implicit `dk <env> up -d` still works but completes only special verbs.

Completion is built at runtime from the repo you are in: deploy environment names (`docker/envs/*/info.yaml` plus `paths.deploy.env_aliases`), `native`/`scripts` extensions, and subcommands are discovered automatically.

## Commands

After install, these console scripts are available:

| Command | Purpose |
|---------|---------|
| `native` | Host development helpers (Django, Vite, fetch, plus `scripts/native-*.sh` extensions) |
| `local` | Deprecated alias for `native` (shell reserved word; prints warning) |
| `dev` | Deprecated alias for `native` (prints warning) |
| `dk` | Docker stack deploy, backup/restore, transfer, Zabbix, DigitalOcean (`dk digoc`), **`dk proxy`** (machine-wide local HTTPS reverse proxy — see [README_DEV_PROXY.md](README_DEV_PROXY.md)), etc. See [Backup and monitoring](#backup-and-monitoring). `dk <env> trust-caddy-cert` / `dk proxy trust` trust Caddy's local HTTPS CA (macOS/Linux). |
| `test` | `backend` / `frontend` / `workspace` pytest or Vitest; **`smoke`** layered stack health + Playwright — see [docs/SMOKE_TESTS.md](docs/SMOKE_TESTS.md) |
| `scripts` | Run `scripts/*.sh` helpers (auto-discovered; excludes `dev-*.sh`) |

### Project script extensions

Place bash scripts under `paths.scripts` in `tooling.yaml` (a single directory or an **ordered list**). When using a list, **earlier directories win** if the same command name appears in more than one path (so project `scripts/` can override shared helpers):

```yaml
paths:
  scripts: scripts
  # Shared postgres helpers (optional second path):
  # scripts:
  #   - scripts
  #   - vendor/postgres/scripts
```

- **`scripts/native-<name>.sh`** → `uv run native <name>`. Deprecated `local-*.sh` / `dev-*.sh` still work with warnings.
- **`scripts/<name>.sh`** (not extension scripts) → `uv run scripts <kebab-name>` (e.g. `fetch_db.sh` → `scripts fetch-db`).
- **`scripts/native-reset-db-post.sh`** — optional hook after `native reset-db`; older hook names deprecated.

Scripts receive `CATALPA_REPO_ROOT`, `CATALPA_FRONTEND_DIR`, and optional Metabase fetch defaults (`FETCH_DB_SSH_HOST`, `FETCH_DB_OUTPUT`) from tooling when run via `uv run scripts …`.

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
- Consumer repo must include a valid `tooling.yaml` (see [tests/fixtures/indmo_reference_tooling.yaml](tests/fixtures/indmo_reference_tooling.yaml) or [tests/fixtures/minimal_project/tooling.yaml](tests/fixtures/minimal_project/tooling.yaml) for examples)
- Host tools for deploy workflows: Docker, `uv`, `sops`, `age` (as needed by your project)
- For DigitalOcean: install the official [doctl](https://docs.digitalocean.com/reference/doctl/) on `PATH` (or set `DOCTL_BIN`). Use **`dk digoc`** for project wrappers (auth, droplets, cloud-config). Run `dk digoc auth init` once per machine. If a stale token is stored, run `dk digoc auth remove --context default` first, or pass a new token with `dk digoc auth init -t TOKEN`

### DigitalOcean PAT scopes

Create a [personal access token](https://docs.digitalocean.com/reference/api/create-personal-access-token/) for the team that owns your projects. `dk` and `dk digoc` call the DigitalOcean API via the host `doctl` binary; insufficient scopes show up as `403` errors.

| What you use | Scopes |
|--------------|--------|
| `dk digoc projects list`, project resolution | `project:read` |
| `dk digoc droplets list`, `dk <env> host` (droplet verify) | `project:read`, `droplet:read` — project-scoped droplet lookup also calls `projects resources list` |
| `dk digoc droplets create`, `dk <env> host create` | above, plus `droplet:create`, **`project:update`** (assign droplet to project after create; doctl `--project-id` uses a second API call), **`ssh_key:read`** (lists keys via `GET /v2/account/keys` — not `account:read`) |
| `dk <env> host create` with `storage.volumes.*.digitalocean` in info.yaml | above, plus `block_storage:read`, `block_storage:create` (create volume), **`block_storage_action:create`** (attach/detach; not `block_storage:update`) |
| `dk <env> host` (DNS verify for `site_origin`) | above, plus `domain:read` |
| `dk <env> host create` (DNS sync after droplet create) | above, plus `domain:write` (or granular domain record create/update) |
| `dk <env> bkp_db` / `bkp_files` auto-provision (missing WRITE creds) | `spaces_key:read`, `spaces_key:create_credentials`; bootstrap may call `spaces keys delete` → `spaces_key:delete` |

`droplet:read`, `droplet:create`, and domain/spaces scopes require companion read scopes (`regions:read`, `sizes:read`, `actions:read`, `image:read`, etc.); the [custom scopes picker](https://cloud.digitalocean.com/account/api/tokens) adds these when you select those scopes. See [Scopes for API tokens](https://docs.digitalocean.com/reference/api/scopes/) for the full list.

**403 on `doctl compute ssh-key list`:** the token is missing **`ssh_key:read`**. That call uses the account keys API (`/v2/account/keys`); [`account:read`](https://docs.digitalocean.com/reference/api/scopes/account/read) is only for profile/billing-style account metadata, not SSH keys. Fix: add `ssh_key:read` to the token, use **Full Access** (`api:write`), or avoid listing by passing explicit keys: `dk <env> host create --ssh-key ID` (repeatable) or `digitalocean.ssh_keys` in `tooling.yaml` (IDs/fingerprints from the control panel or a token that can list keys once).

**Droplet created but `dk <env> host` cannot find it:** DigitalOcean always creates droplets in the account default project first; project membership is a separate assign step. If the droplet appears under the default project (e.g. Internal) instead of `digitalocean.project_name`, the token may lack **`project:update`**. Re-run `dk <env> host create` after fixing the token (it assigns and reuses the existing droplet), or assign manually: `doctl projects resources assign <project-uuid> --resource=do:droplet:<id>`.

**403 on `doctl compute volume-action attach`:** the token is missing **`block_storage_action:create`**. Volume create (`block_storage:create`) and attach are separate scopes; a token that can create or list volumes may still fail on attach. See [`block_storage_action:create`](https://docs.digitalocean.com/reference/api/scopes/block_storage_action/create/).

Convenience aliases (token UI: **Read** / **Full Access**):

- **Read only** — `api:read` — listing projects, droplets, domains, and Spaces keys.
- **Full access** — `api:write` — droplet create, block storage create/attach, DNS sync, Spaces key create, and SSH key listing without picking granular scopes.

PATs are created in the [control panel](https://cloud.digitalocean.com/account/api/tokens) only; there is no API to mint another PAT. Custom scopes you can assign are limited by your team role. A Full Access token can run all `dk` workflows itself — use a separate least-privilege token only when you create it manually in the UI.

**Host tools (not PAT):** Spaces bucket create/check uses host `s3cmd` (`mb`, `info`); credential updates use `sops` — see [README_PGBACKREST.md](README_PGBACKREST.md#auto-provision-digitalocean-spaces).

`dk digoc cloud-config print` does not call the API (no token needed).

### GitHub PAT scopes (GHCR)

`dk push` and `dk clean-images` call the GitHub Container Registry and Packages API. Auth comes from `gh auth token`, or `GH_TOKEN` / `GITHUB_TOKEN`. Insufficient scopes show up as `403` errors.

| What you use | Scopes |
|--------------|--------|
| `dk push` | `write:packages` (push images) |
| `dk clean-images` (dry-run) | **`read:packages`** (list package versions) |
| `dk clean-images --apply` | **`read:packages`**, **`delete:packages`** |

A default `gh auth login` often does **not** include package scopes. Refresh or re-auth:

```bash
gh auth refresh -s read:packages,delete:packages,write:packages
```

For org packages on `ghcr.io/catalpainternational`, use a **classic** PAT with the scopes above (fine-grained tokens may not work for package deletion). Create at [GitHub → Settings → Developer settings → Personal access tokens (classic)](https://github.com/settings/tokens).

**403 on `GET …/packages/container/…/versions`:** the token is missing **`read:packages`**. Dry-run and `--apply` both need it to list versions.

**403 on delete:** add **`delete:packages`**. You also need permission to delete versions in the org (package settings or org admin).

Full usage, retention config, and deploy-tag exclusion: [docs/GHCR_CLEANUP.md](docs/GHCR_CLEANUP.md).

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

By default the DigitalOcean droplet name is **`{project.name}-{env}`** from `tooling.yaml` and the deploy env folder (e.g. `catalpa-site-prod`). Underscores in `project.name` or explicit `droplet_name` values are converted to hyphens (DO hostnames allow only `a-z`, `A-Z`, `0-9`, `.`, and `-`). Override in `docker/envs/<env>/info.yaml`:

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
dk prod host --write      # refresh docker_host from droplet public IPv4 + register SSH host key
dk prod host --sync-dns   # create/update DO A records for site_origin (no known_hosts / verify)
dk digoc droplets list    # includes Env column when tooling.yaml is present
```

After `host create` or `host --write`, the tooling registers the deploy host’s SSH key in your `~/.ssh/known_hosts` (via `ssh-keyscan`, with retries until sshd is reachable) so the next `dk <env> …` command can use `DOCKER_HOST=ssh://…` without a manual first `ssh` login. The same check runs idempotently before other remote `dk` commands when `docker_host` is SSH-formatted, and before `native fetch db` / `native fetch media` when they SSH to a host configured in `tooling.yaml` (legacy `ssh_host`, `fetch_metabase_db.ssh_host`, or the env’s `docker_host`).

**New droplets:** DigitalOcean may report a droplet `active` before SSH accepts connections on port 22. The tooling waits up to ~2 minutes; if registration still fails, `docker_host` is usually already patched — finish with `dk <env> host --write` (or re-run `host create`, which resumes when the droplet already exists).

**Recovery after a partial `host create`:**

| Step | Command |
|------|---------|
| SSH host key | `dk <env> host --write` |
| DO A records only | `dk <env> host --sync-dns` |
| Verify DNS | `dk <env> host` |
| Resume all post-create steps | `dk <env> host create` (reuses existing droplet by default) |
| Droplet in default DO project (e.g. Internal) | Fix token (`project:update`), then `dk <env> host create` or `doctl projects resources assign <project-uuid> --resource=do:droplet:<id>` |

**Default (DigitalOcean):** With doctl, `dk <env> host` checks the droplet exists, status is `active`, and public IPv4 is available; lookup is scoped to `digitalocean.project_name` / `project_id` in `tooling.yaml` when set. When `site_origin` is set, it verifies (1) DigitalOcean DNS API — A records (or CNAMEs that chain to an apex A) on DO-managed zones must point at the droplet IP, zones must be in the project; hostnames not on DO DNS are skipped with a warning — and (2) **public DNS** via the system resolver (Python stdlib, no `dig` required): each `site_origin` hostname must resolve to that IP. `dk <env> host create` creates or updates DO A records after the droplet is active, then runs both checks (not on `host --write`).

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

### Local dev HTTPS proxy (`local_proxy` in `info.yaml`)

For local Docker environments (no `docker_host`), projects may enable a **machine-wide** Caddy reverse proxy that maps a real HTTPS hostname under `*.localdev.temp.build` to the stack's front container. See the topic guides:

| Topic | Guide |
|-------|--------|
| Local dev HTTPS proxy (`local_proxy`, CA trust, `dk proxy`) | [README_DEV_PROXY.md](README_DEV_PROXY.md) |
| LAN access from phones/tablets (sslip.io) | [README_LAN_ACCESS.md](README_LAN_ACCESS.md) |

### Host storage (`storage` in `info.yaml`)

Compose data volumes use stable Docker names (`name: ${COMPOSE_PROJECT_NAME}_…`). Two independent choices:

| Axis | Where | Purpose |
|------|--------|---------|
| **Host path on deploy** | `storage.volumes.<key>.path` in `docker/envs/<env>/info.yaml` | Bind `django_media`, `postgres_data`, or `caddy_data` to a mounted path (optional DO block provisioning) |
| **Volume lifecycle** | `external: true` per volume in the project `compose.yaml` (optional) | Compose fail-fast vs compose-managed create/remove |

When `storage.volumes` is set, tooling pre-creates bind-mounted named volumes before `docker compose up` (`dk up`, `storage ensure`, and post-restore hooks). Host bind placement does **not** require `external: true` in compose.

Example path-only bind (mount configured outside `dk`):

```yaml
storage:
  volumes:
    django_media:
      path: /mnt/btrfs-data/jid-media
```

Optional DigitalOcean block volume provisioning (on `host create` / `storage ensure`):

```yaml
storage:
  volumes:
    django_media:
      path: /mnt/jid-media
      digitalocean:
        size_gib: 200
```

Commands: `dk <env> storage ensure`, `dk <env> ensure_volumes`, `dk <env> up`, and `ops.post_db_restore` hooks after DB restore (ensure volumes before starting the web service).

When tooling pre-creates named volumes for non-`external` compose definitions, it applies Docker Compose metadata labels (`com.docker.compose.project`, `com.docker.compose.volume`) so Compose does not warn that the volume “was not created by Docker Compose”. Volumes created before this behavior (or by plain `docker volume create`) may still warn until removed and recreated (e.g. via `dk <env> wipe`).

## Documentation

| Document | Contents |
|----------|----------|
| [docs/SMOKE_TESTS.md](docs/SMOKE_TESTS.md) | `test smoke` prerequisites, authoring tests, flags |
| [docs/AGENTS_AND_SECRETS.md](docs/AGENTS_AND_SECRETS.md) | `.cursorignore` + Cursor rules (secrets + remote `dk` confirmation) |
| [docs/TYPER_MIGRATION.md](docs/TYPER_MIGRATION.md) | Typer migration audit and low-risk refactor targets |
| [README_PGBACKREST.md](README_PGBACKREST.md) | `pgbr_s3_*` credentials, volume materialize, `bkp_db` |
| [README_RESTIC.md](README_RESTIC.md) | `restic_*` credentials, `bkp_files` |
| [README_SYSTEMD.md](README_SYSTEMD.md) | `ops.systemd_units`, `install-systemd` on deploy hosts |
| [ZABBIX_README.md](ZABBIX_README.md) | `dk <env> zabbix`, UserParameters |

Full onboarding and manifest reference are planned (`ONBOARDING.md`, `CONFIG_REFERENCE.md`). Until then, use an existing consumer’s `tooling.yaml` (see bundled fixtures under `tests/fixtures/`) and that project’s `docker/envs/` layout as a template. Add [Cursor agent guardrails](docs/AGENTS_AND_SECRETS.md) when onboarding a new repo.

## Development

```bash
uv sync --group test
uv run pytest
uv build
```

Releases are tag-driven; see [docs/RELEASING.md](docs/RELEASING.md).

### CLI conventions (Typer-friendly)

The CLIs use argparse today but we intend to migrate to [Typer](https://typer.tiangolo.com/). Keep new/edited command code migration-friendly: put command **logic** in functions that take explicit, typed keyword parameters and keep the argparse layer (`*_parser.py`, `*_cli.py`) as thin glue that reads the namespace and calls them — never pass `argparse.Namespace` into logic. `native_cli.py` and `test_cli.py` already follow this shape. See [`.cursor/rules/typer-compatible-cli.mdc`](.cursor/rules/typer-compatible-cli.mdc) and [docs/TYPER_MIGRATION.md](docs/TYPER_MIGRATION.md) for the audit and refactor priorities.
