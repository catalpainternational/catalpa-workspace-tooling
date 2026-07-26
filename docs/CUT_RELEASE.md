# `dk cut-release` / `dk next-branch`

Guided helpers for app repos that use `dev-X.Y[.Z]` lines and `v*` image tags. Default is **dry-run** (print the plan only). Pass `--execute` to mutate git/GitHub. Never runs remote `dk <env> up`.

This is separate from [releasing catalpa-workspace-tooling itself](RELEASING.md).

## Commands

```bash
dk cut-release final [--execute] [-y] [-C PATH] [--allow-dirty]
dk cut-release beta [W] [--tag TAG] [--execute] [-y] [-C PATH] [--allow-dirty]
dk next-branch <major|minor|hotfix|dev-X.Y[.Z]> [--set-default] [--execute] [-y] [-C PATH] [--allow-dirty]
```

| Verb | Preconditions | Effect |
| --- | --- | --- |
| `cut-release final` | on `dev-X.Y[.Z]` | tag `v`+branch version, merge → `main`, push tag + `main`; print `next-branch` / `image_tag` suggestions |
| `cut-release beta` | on a **named branch** (not detached) | tag `v*.beta.W` on tip; push branch + tag; remind `image_tag` |
| `next-branch` | HEAD at a final `v*` tip (detached **or** any branch) | create/push next `dev-*`; optional `--set-default`; rewrite `.gitmodules` `branch =` when present |

Version is never taken from `info.yaml` / `pyproject` — only from branch/tag names (omit-zeros: patch `0` dropped in names).

## Examples

```bash
# Final release on current dev-* → tag + merge main (dry-run, then execute)
uv run dk cut-release final
uv run dk cut-release final --execute -y

# Open the next line when ready (from the tag tip — often still on main after final)
uv run dk next-branch hotfix
uv run dk next-branch minor --set-default --execute
uv run dk next-branch dev-8.0 --execute

# Staging beta on current tip
uv run dk cut-release beta
uv run dk cut-release beta 3
uv run dk cut-release beta --tag v7.4.1.beta.9   # required on non-dev-* branches
uv run dk cut-release -C bero beta --execute
```

## Beta: branch + tag inference

`beta` must run on a **checked-out named branch** (refuses detached HEAD).

1. Branch matches `dev-X.Y[.Z]` → base `vX.Y[.Z]`, then `.beta.W` (positional `W` or auto max+1).
2. `--tag` on `beta` only:
   - **Override** on a `dev-*` branch (must match `{inferred-v}.beta.*`).
   - **Required** when the branch is **not** parseable as `dev-*`.
3. Positional `W` and `--tag` are mutually exclusive.

`final` has no `--tag` (always `v` + current `dev-*` version).

## Post-cut suggestions

After `cut-release final --execute`, the tool prints how to open the next line (`dk next-branch …`) and reminds you to set `image_tag` in `docker/envs/<env>/info.yaml` yourself. After `beta --execute`, only the `image_tag` / wait-for-CI reminder is printed.

## Shared flags

| Flag | Why |
| --- | --- |
| `--execute` | dry-run default |
| `-y` / `--yes` | skip confirm on execute |
| `-C PATH` | submodule (git-style) |
| `--allow-dirty` | rare escape |
| `--set-default` | `next-branch` only — GitHub default branch |

## Before → after (v1.x reshape)

```bash
# Old                                                         # New
dk cut-release --bump hotfix                                  dk cut-release final
                                                              # then, when ready:
                                                              dk next-branch hotfix

dk cut-release --bump hotfix --image-env staging --execute    dk cut-release final --execute
                                                              # then edit docker/envs/staging/info.yaml image_tag
                                                              dk next-branch hotfix --execute

dk cut-release --bump minor --set-default --execute           dk cut-release final --execute
                                                              dk next-branch minor --set-default --execute

dk cut-release --beta --image-env staging                     dk cut-release beta
dk cut-release --beta --beta-w 3                              dk cut-release beta 3
dk cut-release --submodule bero --beta                        dk cut-release -C bero beta

# detached at v7.4.1 (old Mode B):
dk cut-release --bump minor --execute                         dk next-branch minor --execute
```

`--image-env`, `--pin-submodule`, and `--allow-prod-beta` were removed. Pin submodules in a normal commit before cutting; set `image_tag` in `info.yaml` separately.
