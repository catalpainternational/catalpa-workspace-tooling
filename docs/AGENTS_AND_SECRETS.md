# Cursor agents: secrets and remote environments

Consumer projects using `catalpa-workspace-tooling` store deploy secrets in SOPS-encrypted `docker/envs/<env>/credentials.yaml` and may run Docker on **remote hosts** for staging/production (`docker_host` in `info.yaml`).

**AI coding agents (e.g. Cursor) can include shell output and file reads in model context**, which may be processed on external infrastructure depending on your editor settings.

The tooling itself does **not** upload secrets to Catalpa or third parties. Risks appear when an agent **decrypts secrets**, **prints credentials**, or **runs deploy commands against staging/prod** without the user intending it.

## Recommended setup (every consumer repo)

Install the agent files from the tooling version the project uses:

```bash
uv run setup-agents            # install or update
uv run setup-agents --status   # per file: current, outdated, missing, or owned by the project; exits 1 if any are outdated or missing
uv run setup-agents --dry-run  # show what would change
uv run setup-agents --remove   # remove the files setup-agents manages
```

Then commit what it wrote, so every contributor's agent gets the same guardrails. Run it again after
bumping the tooling: `--status` reports any file the new version would change.

To keep a local version of a managed file, delete its marker line. `setup-agents` then leaves the
file alone, and `--status` reports it as owned by the project rather than failing on it.

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

Each worktree checkout also gets a gitignored **`AGENTS.local.md`** at its root (from `create`; refreshed on `dk worktree up` / `context`) with slug-specific origins and command hints. Add `worktrees` to `agents.rules` when using parallel worktrees.

## Local vs remote environments

| Typical env | `docker_host` | Agent default |
|-------------|---------------|---------------|
| `dev` | (none) | Local Docker — safe for routine dev commands |
| `full` | (none) | Local prod-like stack |
| `staging`, `prod` | `ssh://…` | **Remote** — confirm with user before any `dk` command |

Check `docker/envs/<env>/info.yaml` for `docker_host`. Custom env names follow the same rule: if `docker_host` is set, treat as remote.

## Secrets: what `.cursorignore` excludes

- `docker/envs/**/credentials.yaml` (including SOPS ciphertext)
- `docker/envs/**/dc-backup-tls.yaml` (private backup CA / server PEMs)
- `docker/envs/**/dc-backup.yaml` (Garage rpc/admin secrets)
- `docker/envs/**/*credentials.plain.yaml`
- `.env`, `.env.local`, and other `.env.*` (except `.env.example`)

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
