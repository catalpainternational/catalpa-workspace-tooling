# Changelog

## Unreleased

### Added

- **Closed-DC Garage (`dc-backup`)** — `dk <env> dc-backup tls|bootstrap|install|status|provision`: private CA TLS (`dc-backup-tls.yaml`, inferred `DC_BACKUP_CA_FILE`), Garage rpc/admin secrets (`dc-backup.yaml`), Garage+Caddy compose/mounts on `dc_backup_docker_host`, and `provision` to create bucket/key plus `pgbr_s3_write_*` / `restic_write_*` (or `--print-only`). Command next-step hints point at `provision` / backups; Spaces auto-provision notes Garage when `dc_backup_docker_host` is set. Shared `DOCKER_ADD_HOST` + CA mount for Compose `db`, pgBackRest (`repo1-storage-ca-file`), and restic (`AWS_CA_BUNDLE`). See [README_DC_BACKUP.md](README_DC_BACKUP.md).
- **pgBackRest S3** — optional `pgbr_s3_{write,read}_uri_style` → `repo1-s3-uri-style` and `pgbr_s3_{write,read}_verify_tls` → `repo1-storage-verify-tls` for Garage / MinIO / private-CA endpoints (path-style URLs; TLS verify with mounted CA).

## 0.9.5

### Changed

- Renamed the ``test`` console script to **``tests``** so it no longer collides with the POSIX shell builtin. ``test`` remains as a deprecated alias that prints a warning. Update invocations to ``uv run tests …`` / ``tests …``.

### Fixed

- **`tests smoke`** — when ``local_proxy`` is enabled, run the same proxy sync + ``ports: !reset []`` compose override as ``dk <env> up``, so smoke no longer fails with ``Bind for 0.0.0.0:80 failed`` while ``catalpa-local-proxy`` owns host 80/443.
- **`tests smoke`** — HTTPS ``site_origin`` probes trust the local-proxy CA (``~/.config/catalpa/local-proxy/ca-root.crt``), and smoke pytest inherits a combined ``SSL_CERT_FILE`` so urllib checks against ``*.localdev.temp.build`` no longer fail with ``CERTIFICATE_VERIFY_FAILED``.

## 0.9.4

### Added

- **`test compliance`** — OSS license scan, CycloneDX SBOM generation, `THIRD_PARTY_NOTICES.md`, bundled-asset license checks, and `--check-only` / `--ci` policy gate. See [docs/COMPLIANCE.md](docs/COMPLIANCE.md), `scripts/cursor-rules/oss-compliance.mdc`, and `scripts/compliance-workflow.yml.template`.

### Fixed

- `dk <env> host create` explicitly assigns created droplets to the configured DigitalOcean project and verifies project membership before post-create steps; re-running `host create` can recover droplets orphaned in the account default project.

## 0.9.3

### Added

    - **`dk clean-images`** — remove old GHCR container package versions using retention rules from `docker/images.yaml` (`ghcr_cleanup` block). Dry-run by default; `--apply` deletes after confirmation. Protects deploy pins from `info.yaml` `image_tag` and from SOPS credentials `tag` when decryption works locally. See [docs/GHCR_CLEANUP.md](docs/GHCR_CLEANUP.md) and [README — GitHub PAT scopes (GHCR)](README.md#github-pat-scopes-ghcr).

## 0.9.2

### Added

- **`native.reset_db.restore_as_super`** — opt-in (default `false`) temporary superuser promotion for compose ``pg_restore`` / ``dk transfer``: reload with ``--role APP_USER`` instead of the ``postgis`` default ``--role postgres``. Skipped when ``pg_restore_args`` sets ``--role postgres``.
- **`ops.post_db_restore.db_psql` / `ops.post_metabase_db_restore.db_psql`** — optional Postgres superuser SQL hooks run in the ``db`` container after each restore leg (`target: app` or `metabase`, `file:` repo-relative or in-container path). Runs before `manage_commands`.

### Changed

- **`dk <env> db restore`** — removed hardcoded `grant-cross-db-privileges.sh django-to-metabase` call; permission fixes are project opt-in via `tooling.yaml` hooks only (see bero `manage_commands` pattern).

### Fixed

- **Deployed HTTPS Caddy (staging/prod)** — `dk <env>` now injects `CADDY_SITE_ADDRESS` (and, where applicable, `CADDY_DJANGO_SITE_ADDRESS` / `CADDY_METABASE_SITE_ADDRESS`) as `https://` origins for remote envs (`docker_host: ssh://…`). Previously these were only set behind the local dev proxy, so deployed stacks fell back to the compose `http://…` defaults and Caddy never enabled automatic HTTPS (listening on `:80` only). `CADDY_DJANGO_SITE_ADDRESS` is set for bero stacks only (`paths.frontend: bero`); `CADDY_METABASE_SITE_ADDRESS` only when Metabase is actually routed (explicit `METABASE_ORIGIN` / `METABASE_SITE_ORIGIN`, a `stats` local-proxy role, a bero stack with Metabase fetch configured, or a second `site_origin`). Explicit `info.yaml` `env:` values always win. Local-proxy behavior (`http://` addresses) is unchanged.
- **`dk <env> db restore` / `db pgrestore` / `transfer`** — merge `native.reset_db.pg_restore_args` from `tooling.yaml` into compose `pg_restore` (previously only `native reset-db` / `native pg-restore` honored those flags). Fixes restores that need `--role=postgres` for extension DDL in production dumps (e.g. PostGIS, `pg_stat_statements`).
- **Compose `pg_restore` no longer injects `--no-comments` when `native.reset_db.postgis` is true** — PostGIS prep grants catalog tables to the app user. Opt in to ``native.reset_db.restore_as_super: true`` to temporarily promote ``APP_USER`` to superuser during compose restore, reload with ``--role APP_USER`` (unless ``pg_restore_args`` sets ``--role postgres``), then demote — preserving extension and dbsamizdat object comments without leaving app objects owned by ``postgres``.

## 0.9.1

### Changed

- **Local dev HTTPS proxy (breaking)** — enabled **by default** on local Docker envs (`local_proxy.enabled: false` to opt out). Machine-wide proxy always dials **`{compose_project}-{stack.services.proxy}:80`** (stack Caddy). Removed per-project `local_proxy.service`, `upstream_port`, and manual `routes` lists; use `local_proxy.roles: [admin, stats]` for extra subdomains. Hostnames derive from `{project-slug}-{env}.localdev.temp.build` when `site_origin` is omitted. Compose project name defaults to `{stack.compose_project_default}_{env}`. LAN access remains opt-in (`local_proxy.lan_access: true`).

- **Project-agnostic defaults** — removed hardcoded consumer names (`pas_indmo`, `bero_db`, `INDMO` in Zabbix units, etc.). Tooling now derives compose project names, volume suffixes, smoke DB credentials, healthcheck origin env keys, build placeholders, and Zabbix unit labels from `tooling.yaml`. **`ProjectConfig` is required** when `COMPOSE_PROJECT_NAME` is unset (no silent cross-project fallbacks).

### Added

- **`stack.origin_env_keys`** — env var names probed for in-container HTTP healthcheck `Host` headers (default: `SITE_ORIGIN`, `DJANGO_ORIGIN`, `BERO_ORIGIN`).
- **`stack.build_placeholders`** — compose build-time placeholder values when credentials are not loaded (defaults include generic `SITE_ORIGIN` / `DJANGO_ORIGIN`).
- **`dev:`** section in `tooling.yaml` — optional `site_origin_base`, `lan_dns_suffix`, and `build_time_zone` (defaults: `localdev.temp.build`, `sslip.io`, `Asia/Dili`; `digitalocean.timezone` overrides build timezone when `dev.build_time_zone` is omitted).
- **LAN dev access via local proxy** — with `local_proxy.lan_access` (or `dev_lan_access`), tooling registers sslip.io magic-DNS routes for each LAN IPv4, injects `DOMAIN` / `VITE_EXTRA_ALLOWED_HOSTS`, prints HTTPS LAN URLs on `dk <env> up`, serves the CA at `http://<ip-label>.sslip.io/catalpa-local-ca.crt`, and adds `dk proxy ca` (download URL, terminal QR via `segno`, per-OS install steps). Configurable `local_proxy.lan_dns_suffix` (default `sslip.io`). LAN routes rewrite the upstream `Host` header to the canonical hostname so stack Caddy site blocks and Vite `allowedHosts` match without knowing the dynamic sslip hostname (mirrors how Vite already rewrites Host to `SITE_ORIGIN`).

### Migration (consumer repos)

When bumping to this release, ensure `tooling.yaml` defines at least:

- `stack.compose_project_default`
- `ops.pgbackrest.default_registry` and `stack.images.components`
- `ops.zabbix.unit_name` and `ops.zabbix.userparams_file` (Zabbix unit body now uses these paths and `project.name` in the unit description)

Projects that relied on implicit `bero` / `pas_indmo` defaults must add explicit manifest entries. Re-run `dk <env> zabbix install` on deploy hosts if the unit file still references an old project label or userparams mount path.

**Local proxy:** add stack **Caddy on :80** to dev compose; drop `local_proxy.service` / `upstream_port` / `routes` from `info.yaml`; set `CADDY_*_SITE_ADDRESS` to `http://…`; use `local_proxy.roles` for admin/stats hostnames. Re-run `dk dev up` / `dk full up` and `dk proxy trust` once per machine.

- **Local dev HTTPS proxy** — routes dial project containers over the shared Docker network `catalpa-local-proxy-net` (`{compose_project_name}-{service}:internal_port`) instead of `host.docker.internal` and published host ports. Tooling generates a compose override with `ports: !reset []` and network aliases (requires Docker Compose 2.24+).

## 0.8.5

### Fixed

- **`test smoke` / dev frontend** — poll `site_origin` with retries (up to 120s, 60s per request) instead of a single 10s HTTP GET. Dev stacks serve the webpack dev server on `NODE_PORT`; the first compile can take ~60s while Django is already healthy.

## 0.8.4

### Fixed

- **`test smoke`** — before `docker compose up`, run the same preflight as `dk <env> up`: `ensure_volumes`, local image build when not using a pinned registry, and `materialize_configs`. Fixes failures after `dk dev wipe` when external volumes were missing.

## 0.8.3

### Fixed

- **`test smoke` / stack healthcheck** — in-container HTTP probe now sends a `Host` header derived from `BERO_ORIGIN` (or `SITE_ORIGIN`), fixing timeouts when dev stacks use `*.dev.localhost` while the probe hits `http://localhost:8000/cms/`.
- **`dev_lan_access`** — merge `bero_extra_allowed_hosts` from `info.yaml` with LAN-detected hosts instead of overwriting `BERO_EXTRA_ALLOWED_HOSTS`.

## 0.8.1

### Added

- **`test smoke`** — layered project health checks for Django compose stacks: optional `compose up`, DB wait, in-container `migrate` / `check` / `makemigrations --check`, `stack.healthcheck` web probe, HTTP GET on `site_origin`, then pytest in `{paths.frontend}/smoke`. Flags: `--env`, `--no-up`, `--check-only`, `--fresh-db`, `--ci`. Documented in [docs/SMOKE_TESTS.md](docs/SMOKE_TESTS.md).

### Added

- **LAN dev access** for local `dk dev`: auto-detects the host’s LAN IP and Bonjour `.local` name, injects `BERO_EXTRA_ALLOWED_HOSTS` / `BERO_EXTRA_ORIGINS` for Django, and prints LAN URLs on stack start. Opt out with `dev_lan_access: false` in `docker/envs/dev/info.yaml`. VS Code tasks: **Dev: Show LAN URLs**, **Dev: Open site on LAN**.
- `setup-vscode` CLI: scaffold VS Code tasks for `dk dev` and `dk full` (`uv run setup-vscode`). No SSH-backed fetch tasks. Writes `.vscode/tasks.json`, `extensions.json`, and `settings.json`; patches `.gitignore` for committed VS Code files. Uninstall managed files with `setup-vscode --remove`.
- `setup-shell` adds `compinit` to the catalpa block when `~/.zshrc` has no completion init (and no Oh My Zsh). `setup-shell --status` reports `completion init (compinit)`.
- `catalpa-direnv.zsh` skips tab-completion registration until `compdef` is available (no more `compdef: command not found` when re-sourcing `~/.zshrc`).
- `native fetch db` / `native fetch media` and SSH-backed scripts register deploy host keys in `~/.ssh/known_hosts` automatically (same `ssh-keyscan` flow as `dk` on remote envs). Clearer messages when host-key verification still fails.

### Changed

- `native` loads `paths.env_local` with override so local host Postgres settings (e.g. empty `DJANGO_DB_USER` for Postgres.app) replace inherited project env without adding `.env.local` to direnv.
- `setup-shell` next-steps hint prefers opening a new terminal tab over `source ~/.zshrc`.

## 0.7.7

### Added

- Top-level `setup-shell` CLI: one-time zsh + direnv bootstrap (`uv run setup-shell`). Ships `catalpa-direnv.zsh` in the wheel (`src/catalpa_tooling/shell/`). Uninstall with `setup-shell --remove`. Re-register completion after `source ~/.zshrc` (compinit-safe).

## 0.7.4 (unreleased)

### Added

- `native.fetch_media.legacy.default: true` in tooling.yaml — opt-in default for `--legacy-path` on `native fetch media` (use `--no-legacy-path` to force Docker volume mode).

## 0.6.4 (unreleased)

### Added

- Cursor agent guardrails for consumer repos: [`scripts/cursorignore.template`](scripts/cursorignore.template), [`scripts/cursor-rules/secrets-and-agents.mdc`](scripts/cursor-rules/secrets-and-agents.mdc), [`scripts/cursor-rules/remote-environments.mdc`](scripts/cursor-rules/remote-environments.mdc), and [docs/AGENTS_AND_SECRETS.md](docs/AGENTS_AND_SECRETS.md). README onboarding step recommends copying them into each implementing project.

## 0.5.3

### Fixed

- `native manage` / `native runserver` use plain `uv run ./manage.py` again instead of requiring a `[dependency-groups].dev` table in `paths.backend/pyproject.toml`. Projects that split groups (e.g. `debug`, `test`) no longer need an empty `dev` stub.

## 0.5.1

### Breaking (deprecated aliases retained)

- Host development CLI renamed from `local` to `native` (`uv run native …`). The `local` and `dev` entry points remain but print deprecation warnings (`local` is a shell reserved word).

## 0.5.0

### Breaking (deprecated aliases retained for one release)

- Host development CLI renamed from `dev` to `native` (`uv run native …`). The `dev` and `local` entry points remain but print deprecation warnings (`local` is a shell reserved word).
- `tooling.yaml` section `dev:` / `local:` renamed to `native:` (`NativeConfig`). Loading older keys still works with deprecation warnings.
- `dk <env> bkp_db` / `bkp_files` renamed to `db` / `files`. Old names remain with deprecation warnings.
- Deploy env aliases: `paths.deploy.env_aliases` maps deprecated env names to canonical dirs (e.g. `local: full`).

### Added

- `src/catalpa_tooling/deprecation.py` — shared `warn_deprecated()` helper.
- `docs/NATIVE_COMMAND_TREE.md` (replaces `DEV_COMMAND_TREE.md` / `LOCAL_COMMAND_TREE.md`).

## 0.4.1

### Added

- Unified argparse trees for `dev`, `dk`, `test`, and `scripts` with optional shell completion via `argcomplete` (`[completion]` extra). Register with `register-python-argcomplete` or `scripts/install-completions.sh`.
- Explicit `dk <env> compose …` subcommand for completion-friendly docker compose passthrough; implicit `dk <env> up -d` remains supported.
- Multi-project direnv integration: [`scripts/catalpa-direnv.zsh`](scripts/catalpa-direnv.zsh) (one-time zsh hook) and [`scripts/envrc.template`](scripts/envrc.template) (per-repo `.envrc`).

### Fixed

- Spaces backup auto-provisioning assigns the bucket to the DigitalOcean project from `digitalocean.project_name` / `project_id` in `tooling.yaml` (via `doctl projects resources assign do:space:<bucket>`), not only the default project.

## 0.3.0

### Added

- Built-in `dk <env> trust-caddy-cert` (macOS): trusts Caddy's local development CA in the System keychain using `stack.services.proxy` and the env's compose file. Projects can remove `scripts/trust-caddy-cert.sh`.
- `dk <env> host --sync-dns` to create or update DigitalOcean A records without touching `known_hosts` or running full verification.

### Fixed

- `dk <env> host --write` no longer aborts when DigitalOcean or public DNS checks fail; it only refreshes `docker_host` from the droplet (DNS checks still run on plain `dk <env> host`).
- DigitalOcean DNS verification accepts `www` (and other) **CNAME** records that chain to an apex **A** record on the droplet IP; `host create` DNS sync no longer replaces existing CNAMEs with A records.
- `ssh-keyscan` after `host create` / `host --write` retries until SSH is reachable (new droplets often need time after DO reports `active`). `host create` continues DNS sync even when host-key registration fails, and re-running `host create` finishes provisioning when the droplet already exists (`--no-reuse-existing` to opt out).
- Public DNS verification prints hints on resolver failure (propagation, `host --sync-dns`, macOS cache flush) when `nslookup` may already succeed.

## 0.2.0

### Breaking

- Removed built-in `dev storybook` and `dev prototype` subcommands. Add `scripts/dev-storybook.sh` and `scripts/dev-prototype.sh` in your repo (see `share/npm-run.sh`).
- Removed built-in `scripts merge-tetum-po`. Use a `scripts/*.sh` file; the CLI name is derived from the filename (e.g. `merge-tetum-transifex.sh` → `merge-tetum-transifex`).
- `dev reset-db` no longer runs `load_seed_content` by default. It runs `migrate` only, unless `scripts/dev-reset-db-post.sh` exists (project hook for migrate/seed or other steps).

### Added

- Auto-discover `scripts/dev-*.sh` as `uv run dev <name>` subcommands.
- Auto-discover other `scripts/*.sh` as `uv run scripts <kebab-name>` subcommands.
- Bundled `share/npm-run.sh` (`npm_run_in_dir`) for project dev scripts.

## 0.1.5

Previous release.
