# GHCR image cleanup

Remove old container package versions from GitHub Container Registry with `dk clean-images`. The command reads retention rules from each project's `docker/images.yaml` and protects deploy pins from `docker/envs/*/info.yaml` (and from SOPS credentials when available locally).

## Prerequisites

Authenticate with GitHub using one of:

- `gh auth login` (classic PAT recommended for org packages)
- `GH_TOKEN` or `GITHUB_TOKEN` in the environment

The token needs **`read:packages`** and **`delete:packages`** scopes.

## Usage

Dry-run (default) — shows what would be deleted:

```bash
uv run dk clean-images
```

Apply deletions (with confirmation prompt):

```bash
uv run dk clean-images --apply
```

Non-interactive apply:

```bash
uv run dk clean-images --apply --yes
```

Clean a single package:

```bash
uv run dk clean-images --package catalpa_bero-django
```

Override retention for one run:

```bash
uv run dk clean-images --keep-n-tagged 10 --older-than "90 days"
```

## Project configuration

In `docker/images.yaml`:

```yaml
image_registry: ghcr.io/catalpainternational

ghcr_cleanup:
  keep_n_tagged: 20
  older_than: 180 days
  delete_untagged: true
  extra_exclude_tags: []
  collect_deploy_tags: true
```

Package names come from `tooling.yaml` → `stack.images.components` (the same names `dk push` publishes).

Set non-secret deploy pins in each environment's `docker/envs/<env>/info.yaml`:

```yaml
image_tag: v2.8.2a
```

When `collect_deploy_tags: true`, those tags are always excluded. If `image_tag` is not set in `info.yaml`, `dk clean-images` also reads `tag`, `tag_db`, and `tag_caddy` from `credentials.yaml` when SOPS decryption works on your machine (typical for prod).

## Retention rules

For each stack package:

1. Versions whose tags match `exclude_tags` (deploy pins + `extra_exclude_tags`, wildcards supported) are kept.
2. Tagged versions newer than `older_than` are kept.
3. Among older tagged versions, the `keep_n_tagged` newest are kept; the rest are deleted.
4. Untagged versions are deleted when `delete_untagged: true`.

## Restoring deleted versions

The command logs each deleted package version ID. GitHub allows restoring deleted package versions via the REST API for a limited time — see [Restore organization package version](https://docs.github.com/en/rest/packages/packages#restore-package-version-for-an-organization).

## Operational playbook

1. Ensure staging/prod `image_tag` values in `info.yaml` match what is deployed.
2. Run `uv run dk clean-images` and review the output.
3. Run `uv run dk clean-images --apply` when satisfied.
4. Re-run after major releases or when registry storage grows.

For hotfix tags outside the retention window, add them temporarily to `extra_exclude_tags` in `docker/images.yaml`.
