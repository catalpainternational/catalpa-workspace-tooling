# Changelog

## 0.3.0

### Added

- Built-in `dk <env> trust-caddy-cert` (macOS): trusts Caddy's local development CA in the System keychain using `stack.services.proxy` and the env's compose file. Projects can remove `scripts/trust-caddy-cert.sh`.

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
