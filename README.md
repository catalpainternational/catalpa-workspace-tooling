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

## Documentation

Full onboarding and manifest reference are planned (`ONBOARDING.md`, `CONFIG_REFERENCE.md`). Until then, use an existing consumer’s `tooling.yaml` and deploy docs as a template.

## Development

```bash
uv sync --group test
uv run pytest
uv build
```
