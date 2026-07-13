# Typer migration audit

The CLIs (`dk`, `native`, `scripts`, `tests`, …) use **argparse** today. We intend to migrate to [Typer](https://typer.tiangolo.com/) eventually. This document records an audit of where the codebase already matches a Typer-friendly shape, and where low-risk refactors would help before a full migration.

**Conventions for new code:** see [`.cursor/rules/typer-compatible-cli.mdc`](../.cursor/rules/typer-compatible-cli.mdc).

## Why migration is non-trivial

Typer generates a command call with **one explicit, typed parameter per option/argument**. Handlers that receive an `argparse.Namespace` and read values with `ns.x` or `getattr(ns, "x", …)` cannot map 1:1 to a Typer command. The heavy lift is untangling those handlers — especially large dispatchers that branch on `ns.<subcommand>_command`.

**Target shape:**

- Command **logic** = functions with explicit, typed keyword parameters (`str`, `bool`, `int`, `Path`, `list[str]`, `Enum`/`Literal` for choices).
- **Parsing** (`*_parser.py`, `*_cli.py`) = thin glue that builds the parser, reads the namespace once, and calls the logic function.

## Already Typer-shaped (reference implementations)

These entrypoints already follow the pattern. Use them as examples when adding or editing commands.

| Module | Pattern |
|--------|---------|
| `native_cli.py` | Reads `args`, calls `_cmd_fetch_db(output=…, dk_env=…)`, `_cmd_fetch_media(host=…, …)`, `_run_reset_db_drop_create_migrate_seed(from_dump=…, …)` |
| `test_cli.py` | Reads `args`, calls `run_smoke(env_name=…, no_up=…, …)` |
| `scripts_cli.py` | Reads `args`, calls `run_bash_script(…)` |

## Low risk — do these first

Small, mechanical changes. Each handler reads only one or two namespace fields at the top; extract them into explicit keyword parameters and keep a thin adapter.

| Priority | Module / symbol | Namespace usage | Suggested change |
|----------|-----------------|-----------------|------------------|
| 1 | `local_compose.py` — `_cmd_build` | `ns.services` only | `build_stack(config, *, services: tuple[str, …] \| None = None)`; drop unused `_compose_file` param |
| 2 | `build_push.py` — `_cmd_push` | `ns.registry`, `ns.tag` | `push_stack(config, *, registry: str \| None = None, tag: str \| None = None)`; drop unused `_compose_file` |
| 3 | `local_proxy_cli.py` — `cmd_proxy` | `ns.proxy_command`, `ns.dry_run` | Split into `proxy_up`, `proxy_down`, `proxy_status`, `proxy_trust` (each `*, dry_run: bool = False` where relevant) |

`dk_cli.py` dispatch for `build` / `push` / `proxy` would pass `ns.*` into these functions — the only place that touches the namespace.

## Low–medium risk

| Module / symbol | Notes |
|-----------------|-------|
| `dk_transfer.py` — `cmd_transfer` | Reads ~8 flat fields at the top (`source_env`, `dest_env`, `dry_run`, `yes`, `db`, `media`, `workdir`, `keep_workdir`). Body is long but namespace reads are localized. Wrap as `run_transfer(config, *, source_env, dest_env, …)` with thin `cmd_transfer(ns, config)` adapter. Good standalone PR. |

## Medium risk — worthwhile during migration prep

| Module / symbol | Notes |
|-----------------|-------|
| `doctl_cli.py` | Per-subcommand handlers already exist (`_cmd_auth_init`, `_cmd_projects_list`, `_cmd_droplets_create`, …). Many accept `ns_or_argv` and read `ns` internally. `_forward_host_create_argv(ns)` rebuilds argv from a namespace (anti-pattern). Migrate one handler at a time to explicit parameters. |

## High risk — leave for dedicated migration

| Module / symbol | Notes |
|-----------------|-------|
| `env_handlers.py` | `handle_env_command`, `_handle_bkp_files`, `_handle_bkp_db`, `_zabbix_argv_from_ns`. Dozens of `ns` / `getattr(ns, …)` reads; deep branching on `ns.env_command` and nested `*_command` fields. This is the bulk of the “heavy refactor”; tackle during the Typer migration, not opportunistically. |

## Suggested order of work

1. **Items 1–3** (low risk) — safe, isolated wins; reinforce the convention with concrete in-repo examples.
2. **`dk transfer`** — single PR when convenient.
3. **`doctl_cli.py`** — one handler per PR during migration prep.
4. **`env_handlers.py`** — as part of the actual Typer cutover (likely split by env subcommand tree).

## Out of scope for this audit

- Replacing **argcomplete** with Typer/shell completion (separate decision).
- Changing console script entrypoints in `pyproject.toml` until migration is ready.
- Big-bang rewrite of `*_parser.py` trees — migrate dispatch + logic first; parsers can stay on argparse until handlers are clean.

## How this doc stays current

When you refactor a handler toward explicit parameters, update the table row (move it to “Already Typer-shaped” or remove it). When you add a new namespace-based handler, note it here so we do not accidentally grow the high-risk surface.
