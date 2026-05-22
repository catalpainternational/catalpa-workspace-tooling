# Changelog

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
