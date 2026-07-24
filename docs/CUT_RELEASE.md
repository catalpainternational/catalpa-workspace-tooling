# `dk cut-release`

Guided cutover helper for app repos that use `dev-X.Y[.Z]` lines and `v*` image tags. Default is **dry-run** (print the plan only). Pass `--execute` to mutate git/GitHub. Never runs remote `dk <env> up`.

This is separate from [releasing catalpa-workspace-tooling itself](RELEASING.md) (`scripts/release.sh`).

## How mode and version are chosen

The command does **not** take a free-form version argument. It inspects **HEAD** in the target git repo (repo root, or `--submodule PATH`), then picks a mode and a base version from that state.

### HEAD → mode

| Checked-out state                              | Flags                       | Mode                     |
| ---------------------------------------------- | --------------------------- | ------------------------ |
| Branch matching `dev-X.Y` or `dev-X.Y.Z`       | `--bump` or `--next-branch` | **A** — final release    |
| Detached HEAD at a final `vX.Y` / `vX.Y.Z` tag | `--bump` or `--next-branch` | **B** — next branch only |
| Branch matching `dev-X.Y[.Z]`                  | `--beta`                    | **C** — staging beta     |

Rules that matter in practice:

- Mode is decided from the **current branch name** or from a **tag that points at HEAD**, not from `info.yaml`, `pyproject.toml`, or CI.
- Mode B requires a **detached** checkout of the tag (`git checkout v7.4.1`). If you are on a named branch (even one that points at the same commit as a `v*` tag), Mode B does not apply.
- If HEAD is a `dev-*` branch, Mode A/C wins even when that commit is also tagged.
- Anything else (e.g. `main`, `feature/…`, or detached HEAD with no usable `v*` tag) fails with an error naming the branch/tag that was seen.

### Where the version numbers come from

| Mode  | Base version (`X.Y[.Z]`)                                                             | Release / beta tag                                                    | Next `dev-*` branch                                          |
| ----- | ------------------------------------------------------------------------------------ | --------------------------------------------------------------------- | ------------------------------------------------------------ |
| **A** | Parsed from the current branch: `dev-7.4.1` → `7.4.1`, `dev-7.5` → `7.5` (patch `0`) | Default `v` + that version (`v7.4.1`, `v7.5`). Override with `--tag`. | From `--bump` (or `--next-branch`) applied to that version   |
| **B** | Parsed from the **exact final `v*` tag at HEAD**                                     | None (no new tag)                                                     | From `--bump` / `--next-branch` applied to the tag’s version |
| **C** | Parsed from the current `dev-*` branch (same as A)                                   | `vX.Y[.Z].beta.W` — see below                                         | None                                                         |

**Omit-zeros naming:** patch `0` is omitted in branch and tag names (`7.5` / `v7.5` / `dev-7.5`, not `7.5.0`). Non-zero patch is always written (`7.4.1` / `v7.4.1` / `dev-7.4.1`). The same rule is used when formatting the _next_ branch after a bump.

**Accepted patterns** (full string match; no prefixes/suffixes):

- Branches: `dev-<major>.<minor>` or `dev-<major>.<minor>.<patch>`
- Final tags: `v<major>.<minor>` or `v<major>.<minor>.<patch>`
- Beta tags: `v<major>.<minor>[.<patch>].beta.<W>` with `W >= 1`

### Tag at HEAD (Mode B detail)

When HEAD is detached, the command lists tags that **point at the current commit**:

1. Prefer tags that parse as a **final** release (`vX.Y` / `vX.Y.Z`), ignoring `*.beta.*`.
2. If exactly one such final tag exists, use it.
3. If several final tags point at the same commit, pick one deterministically (longer name first, then lexicographic).
4. If there are no final `v*` tags but exactly one tag of any kind points at HEAD, that single tag is returned — Mode B still only proceeds if it parses as a final `v*`.
5. If there is no usable final tag, Mode B is not selected.

So: checkout the release tag you care about (`git checkout v7.4.1`), then run `dk cut-release --bump …`.

### Beta `W` (Mode C detail)

Without `--tag` / `--beta-w`, `W` is **max existing `vX.Y[.Z].beta.*` for that version in the local tag list, plus 1**. Existing tags are read with `git tag -l` (run a fetch first if your local tags are stale). Examples on `dev-7.4.1`:

- no betas yet → `v7.4.1.beta.1`
- `v7.4.1.beta.1` and `.beta.2` exist → `v7.4.1.beta.3`

`--beta-w N` forces `W`. `--tag` must be exactly `{v-version}.beta.W` for the current branch version (e.g. on `dev-7.4.1`, `--tag v7.4.1.beta.9`).

### Overrides

| Flag                 | Effect                                                                                                                                                                 |
| -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--tag TAG`          | Mode A: use this as the release tag instead of `v` + branch version. Mode C: must match the beta pattern for the branch version.                                       |
| `--next-branch NAME` | Mode A/B: use this next branch name instead of computing it from `--bump`. Mode A still requires `--bump` _or_ `--next-branch`. If both are set, `--next-branch` wins. |
| `--beta-w N`         | Mode C: explicit beta index (default: auto-increment).                                                                                                                 |

`--bump` and `--beta` are mutually exclusive. `--beta` also rejects `--set-default` and `--next-branch`.

## Modes

### A — Final release (on `dev-X.Y[.Z]`)

```bash
uv run dk cut-release --bump hotfix          # dry-run
uv run dk cut-release --bump hotfix --execute --set-default
```

- Version from branch name; tag defaults to `v` + that version (`dev-7.4.1` → `v7.4.1`)
- Merge ready branch → `main`, push annotated tag
- Next branch from `--bump` (classic semver, omit-zeros):

| `--bump` | From `7.4.1` | Next branch |
| -------- | ------------ | ----------- |
| `major`  | →            | `dev-8.0`   |
| `minor`  | →            | `dev-7.5`   |
| `hotfix` | →            | `dev-7.4.2` |

On execute (after confirmation): refuse a dirty tree unless `--allow-dirty`; `git fetch --tags --prune origin`; optional submodule pins and `image_tag` commit; rewrite `.gitmodules` `branch =` lines that still name the ready line; checkout `main`, ff-only pull, merge the ready branch (ff-only, falling back to `--no-ff`); annotated tag; push `main` + tag; create/push the next `dev-*` from the tag tip; optional `gh` default-branch update via `--set-default`.

Optional: `--image-env staging` updates `docker/envs/<env>/info.yaml` `image_tag` to the new release tag. Submodule `branch =` entries that still name the ready line are rewritten to the next branch when `.gitmodules` is present.

### B — Next line only (on a `vX.Y[.Z]` tag)

```bash
git checkout v7.4.1   # detached HEAD at the final tag
uv run dk cut-release --bump minor --execute --set-default
```

Version comes from that tag. Creates/pushes the next `dev-*` from the tag tip. No new tag, no merge to `main`, no `.gitmodules` rewrite.

### C — Staging beta (on `dev-X.Y[.Z]`)

```bash
uv run dk cut-release --beta --image-env staging
uv run dk cut-release --beta --image-env staging --execute
```

- Version from the `dev-*` branch; tags **branch tip** as `vX.Y[.Z].beta.W` (auto-increment `W`, or `--beta-w` / `--tag`)
- Does **not** merge to `main`, create a next branch, or change the default branch
- Refuses `--image-env prod` unless `--allow-prod-beta`
- Incompatible with `--bump` / `--set-default` / `--next-branch`

Typical loop: cut beta → wait for CI `v*` image push → manually deploy staging → fix → cut next beta → when verified, Mode A for the final tag.

## Safety and defaults

| Behavior           | Default                                                                                              |
| ------------------ | ---------------------------------------------------------------------------------------------------- |
| Dry-run vs mutate  | Plan only, unless `--execute`                                                                        |
| Confirmation       | Prompt `[y/N]` on `--execute`, unless `-y` / `--yes`                                                 |
| Dirty working tree | `--execute` fails unless `--allow-dirty` (prints porcelain status)                                   |
| Remote deploy      | Never; you run `dk <env> up` yourself after CI publishes images                                      |
| Target repo        | App repo root, or `--submodule PATH` (e.g. `bero`)                                                   |
| `--set-default`    | Off; when set (Mode A/B only), runs `gh repo edit … --default-branch`                                |
| `--image-env`      | Off; when set, writes `image_tag` in `docker/envs/<NAME>/info.yaml` to the new tag (release or beta) |

## Common flags

| Flag                       | Purpose                                                                                              |
| -------------------------- | ---------------------------------------------------------------------------------------------------- |
| `--submodule PATH`         | Run inside a submodule (e.g. `bero`)                                                                 |
| `--pin-submodule PATH=REF` | Detach-checkout REF in submodule and commit the gitlink (repeatable; applied before tagging/merging) |
| `--tag` / `--next-branch`  | Explicit overrides (see above)                                                                       |
| `--beta-w N`               | Explicit beta index (Mode C)                                                                         |
| `-y` / `--yes`             | Skip confirmation when using `--execute`                                                             |
| `--allow-dirty`            | Allow `--execute` with uncommitted changes (prints porcelain status)                                 |
| `--allow-prod-beta`        | Allow `--image-env prod` together with `--beta`                                                      |

## Examples

```bash
# Inspect the plan without touching git (always start here)
uv run dk cut-release --bump hotfix

# Bero cutover from a consumer checkout
uv run dk cut-release --submodule bero --beta
uv run dk cut-release --submodule bero --bump hotfix --set-default --execute

# Mode B: open the next line from an already-cut tag
git checkout v7.4.1
uv run dk cut-release --bump minor --execute --set-default

# Consumer release after a bero tag exists
uv run dk cut-release --bump hotfix --set-default \
  --pin-submodule bero=v7.4.1 \
  --image-env staging \
  --execute
```
