# `dk cut-release`

Guided cutover helper for app repos that use `dev-X.Y[.Z]` lines and `v*` image tags. Default is **dry-run** (print the plan only). Pass `--execute` to mutate git/GitHub. Never runs remote `dk <env> up`.

This is separate from [releasing catalpa-workspace-tooling itself](RELEASING.md) (`scripts/release.sh`).

## Modes

### A — Final release (on `dev-X.Y[.Z]`)

```bash
uv run dk cut-release --bump hotfix          # dry-run
uv run dk cut-release --bump hotfix --execute --set-default
```

- Tag = `v` + branch version (`dev-7.4.1` → `v7.4.1`)
- Merge ready branch → `main`, push annotated tag
- Next branch from `--bump` (classic semver, omit-zeros):

| `--bump` | From `7.4.1` | Next branch |
|----------|--------------|-------------|
| `major` | → | `dev-8.0` |
| `minor` | → | `dev-7.5` |
| `hotfix` | → | `dev-7.4.2` |

Optional: `--image-env staging` updates `docker/envs/<env>/info.yaml` `image_tag`. Submodule `branch =` entries that still name the ready line are rewritten to the next branch when `.gitmodules` is present.

### B — Next line only (on a `vX.Y[.Z]` tag)

```bash
uv run dk cut-release --bump minor --execute --set-default
```

Creates/pushes the next `dev-*` from the tag tip. No new tag.

### C — Staging beta (on `dev-X.Y[.Z]`)

```bash
uv run dk cut-release --beta --image-env staging
uv run dk cut-release --beta --image-env staging --execute
```

- Tags **branch tip** as `vX.Y.Z.beta.W` (auto-increment `W`, or `--beta-w N`)
- Does **not** merge to `main`, create a next branch, or change the default branch
- Refuses `--image-env prod` unless `--allow-prod-beta`
- Incompatible with `--bump` / `--set-default` / `--next-branch`

Typical loop: cut beta → wait for CI `v*` image push → manually deploy staging → fix → cut next beta → when verified, Mode A for the final tag.

## Common flags

| Flag | Purpose |
|------|---------|
| `--submodule PATH` | Run inside a submodule (e.g. `bero`) |
| `--pin-submodule PATH=REF` | Detach-checkout REF in submodule and commit the gitlink (repeatable) |
| `--tag` / `--next-branch` | Explicit overrides |
| `-y` / `--yes` | Skip confirmation when using `--execute` |
| `--allow-dirty` | Allow `--execute` with uncommitted changes (prints porcelain status) |

## Examples

```bash
# Bero cutover from a consumer checkout
uv run dk cut-release --submodule bero --beta
uv run dk cut-release --submodule bero --bump hotfix --set-default --execute

# Consumer release after a bero tag exists
uv run dk cut-release --bump hotfix --set-default \
  --pin-submodule bero=v7.4.1 \
  --image-env staging \
  --execute
```
