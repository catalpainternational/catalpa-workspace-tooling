# Coding agents (Cursor, Claude Code): secrets and remote environments

Consumer projects using `catalpa-workspace-tooling` store deploy secrets in SOPS-encrypted `docker/envs/<env>/credentials.yaml` and may run Docker on **remote hosts** for staging/production (`docker_host` in `info.yaml`).

**AI coding agents (e.g. Cursor, Claude Code) can include shell output and file reads in model context**, which may be processed on external infrastructure depending on your editor settings.

The tooling itself does **not** upload secrets to Catalpa or third parties. Risks appear when an agent **decrypts secrets**, **prints credentials**, or **runs deploy commands against staging/prod** without the user intending it.

## Recommended setup (every consumer repo)

Install the agent files from the tooling version the project uses:

```bash
uv run setup-agents            # install or update
uv run setup-agents --status   # present / managed / current per file; exits 1 unless all are
uv run setup-agents --dry-run  # show what would change
uv run setup-agents --remove   # remove the files setup-agents manages
```

Then commit what it wrote, so every contributor's agent gets the same guardrails. Run it again after
bumping the tooling: `--status` reports any file the new version would change.

The rules and `.cursorignore` are always installed. Optional rules and skills are chosen in
`tooling.yaml`:

```yaml
agents:
  rules: [worktrees, smoke-tests]   # optional rules; default none
  skills: [dk-stack]                # default [dk-stack]; [] for none
```

Removing a name later removes the file it installed. Skills go in `.claude/skills/<name>/`, read by
Claude Code, with a `.cursor/skills/<name>` symlink to the same directory for Cursor.

| File | Purpose |
|------|---------|
| [`.cursorignore`](../src/catalpa_tooling/share/agents/cursorignore) | Excludes credential and env files from agent indexing |
| [`secrets-and-agents.mdc`](../src/catalpa_tooling/share/agents/rules/secrets-and-agents.mdc) | No decrypt/show of SOPS or env secrets |
| [`remote-environments.mdc`](../src/catalpa_tooling/share/agents/rules/remote-environments.mdc) | No `dk staging`/`prod`, push, transfer, `clean-images --apply`, or fetch-from-prod without user confirmation |
| [`smoke-tests.mdc`](../src/catalpa_tooling/share/agents/rules/smoke-tests.mdc) | Optional (`smoke-tests`) — `tests smoke` setup and when to run after tooling bumps ([SMOKE_TESTS.md](SMOKE_TESTS.md)) |
| [`worktrees.mdc`](../src/catalpa_tooling/share/agents/rules/worktrees.mdc) | Optional (`worktrees`) — `dk worktree` for isolated local worktrees ([WORKTREES.md](WORKTREES.md)) |
| [`oss-compliance.mdc`](../src/catalpa_tooling/share/agents/rules/oss-compliance.mdc) | Optional (`oss-compliance`) — `tests compliance` after dependency bumps ([COMPLIANCE.md](COMPLIANCE.md)) |
| [`dk-stack` skill](../src/catalpa_tooling/share/agents/skills/dk-stack/SKILL.md) | Default skill — how an agent brings up, reaches and checks the local `dk dev` stack. Project specifics go in the consumer's own `run-app` skill beside it |

### Managed files

Each file `setup-agents` writes carries a line saying so (an HTML comment after the frontmatter
in `.mdc` and `SKILL.md`, a `#` comment in `.cursorignore`). Those files are the tooling's: local
edits are overwritten on the next run. To keep a local version, delete that line. The file is then
the project's, and `setup-agents` leaves it alone unless run with `--force`.

A file that already exists without the line is left alone too, unless it is identical to the
bundled copy (a rule copied by hand from the same version), in which case it is adopted. Copies
from an older version need `--force` once. A real directory where a skill's `.cursor/skills`
symlink would go is never replaced.

### Claude Code

Claude Code reads neither `.cursor/rules/` nor `.cursorignore`, so `setup-agents` gives it the same
guardrails in its own formats:

| Cursor | Claude Code | How |
|--------|-------------|-----|
| `.cursor/rules/<rule>.mdc` | `.claude/rules/<rule>.md` | Converted from the same source. A rule scoped by `globs` gets them as `paths:`. Every other rule loads at each session start, including ones Cursor applies only when the agent picks them by description (`worktrees`) |
| `.cursorignore` | `Read(...)` deny rules in `.claude/settings.json` | One rule per pattern. A pattern with a `/` is anchored at the repo root (`Read(/docker/envs/**/credentials.yaml)`), and one without matches at any depth (`Read(.env)`) |

`.claude/settings.json` is the project's file too: allow rules, env, hooks. `setup-agents` only adds
its own deny entries to `permissions.deny`, leaving everything else as it is, and `--remove` takes
out exactly those entries (deleting the file only if nothing else is left). A file that isn't a
JSON object is reported and left alone; `--force` doesn't change that.

What the deny rules do not do:

- **They're not an index exclusion.** They block Claude Code's file tools and the shell commands
  it recognizes as file reads (`cat`, `head`, `sed`, redirections). They don't block a `grep -r`
  across the tree, or a script that opens the file itself. `secrets-and-agents` still says not to
  try, and Claude Code's sandbox (`sandbox.filesystem.denyRead`) is the OS-level control if a
  project wants one.
- **They have no exceptions.** `!.env.example` keeps `.env.example` visible to Cursor, but
  `Read(.env.*)` hides it from Claude Code.

Each worktree checkout also gets a gitignored **`AGENTS.local.md`** at its root (from `create`; refreshed on `dk worktree up` / `context`) with slug-specific origins and command hints. Add `worktrees` to `agents.rules` when using parallel worktrees.

## Local vs remote environments

| Typical env | `docker_host` | Agent default |
|-------------|---------------|---------------|
| `dev` | (none) | Local Docker — safe for routine dev commands |
| `full` | (none) | Local prod-like stack |
| `staging`, `prod` | `ssh://…` | **Remote** — confirm with user before any `dk` command |

Check `docker/envs/<env>/info.yaml` for `docker_host`. Custom env names follow the same rule: if `docker_host` is set, treat as remote.

## Secrets: what `.cursorignore` and the Claude Code deny rules exclude

- `docker/envs/**/credentials.yaml` (including SOPS ciphertext)
- `docker/envs/**/dc-backup-tls.yaml` (private backup CA / server PEMs)
- `docker/envs/**/dc-backup.yaml` (Garage rpc/admin secrets)
- `docker/envs/**/*credentials.plain.yaml`
- `.env`, `.env.local`, and other `.env.*` (except `.env.example`, for Cursor only)

Adjust paths if your manifest uses different `paths.deploy.envs_dir` or `paths.env_local`.

## Safe validation (agents and humans)

```bash
# Encrypted credentials exist
test -f docker/envs/staging/credentials.yaml

# SOPS decrypt works — no secret printed
sops -d docker/envs/staging/credentials.yaml >/dev/null && echo "SOPS OK"

# Compose valid locally (not on staging)
uv run dk dev config --services
```

Do **not** use `sops -d … | head`, `gopass show`, or `dk staging config` to “inspect” secrets or expanded remote env in an agent session.

## Remote commands that need user confirmation

Examples (non-exhaustive):

- `uv run dk staging up -d`, `uv run dk prod down`
- `uv run dk push`, `uv run dk transfer`
- `uv run dk clean-images --apply` (deletes GHCR package versions; dry-run without `--apply` is read-only but still needs a GitHub token with `read:packages`)
- `uv run native fetch db`, `uv run native fetch media`
- `ssh` to hosts named in `info.yaml`

## Dev environments

Some projects put low-risk dev defaults in `docker/envs/dev/info.yaml`. OAuth client secrets should live in optional dev `credentials.yaml` or encrypted files when possible. Agents can still read plaintext `info.yaml` — keep production-like secrets out of it.

## Related docs

- [QUICKSTART.md](../QUICKSTART.md) — consumer install, first droplet / deploy, remote-env confirmation after `docker_host`
- [README.md](../README.md) — [GitHub PAT scopes (GHCR)](../README.md#github-pat-scopes-ghcr), `site_origin`
- [GHCR_CLEANUP.md](GHCR_CLEANUP.md) — `dk clean-images` usage and retention config
- [SMOKE_TESTS.md](SMOKE_TESTS.md) — `tests smoke` contract and authoring tests
- Consumer `docker/envs/README.md` — per-env credentials bootstrap
