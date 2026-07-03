# Cursor agents: secrets and remote environments

Consumer projects using `catalpa-workspace-tooling` store deploy secrets in SOPS-encrypted `docker/envs/<env>/credentials.yaml` and may run Docker on **remote hosts** for staging/production (`docker_host` in `info.yaml`).

**AI coding agents (e.g. Cursor) can include shell output and file reads in model context**, which may be processed on external infrastructure depending on your editor settings.

The tooling itself does **not** upload secrets to Catalpa or third parties. Risks appear when an agent **decrypts secrets**, **prints credentials**, or **runs deploy commands against staging/prod** without the user intending it.

## Recommended setup (every consumer repo)

Copy the templates from this package (or from an existing consumer such as JID):

```bash
# From a catalpa-workspace-tooling checkout (adjust path):
cp /path/to/catalpa-workspace-tooling/scripts/cursorignore.template .cursorignore
mkdir -p .cursor/rules
cp /path/to/catalpa-workspace-tooling/scripts/cursor-rules/secrets-and-agents.mdc .cursor/rules/
cp /path/to/catalpa-workspace-tooling/scripts/cursor-rules/remote-environments.mdc .cursor/rules/
# Optional — smoke test hints for agents (bero / Django compose consumers):
cp /path/to/catalpa-workspace-tooling/scripts/cursor-rules/smoke-tests.mdc .cursor/rules/
```

| File | Purpose |
|------|---------|
| [`.cursorignore`](../scripts/cursorignore.template) | Excludes credential and env files from agent indexing |
| [`secrets-and-agents.mdc`](../scripts/cursor-rules/secrets-and-agents.mdc) | No decrypt/show of SOPS or env secrets |
| [`remote-environments.mdc`](../scripts/cursor-rules/remote-environments.mdc) | No `dk staging`/`prod`, push, transfer, or fetch-from-prod without user confirmation |
| [`smoke-tests.mdc`](../scripts/cursor-rules/smoke-tests.mdc) | Optional — `test smoke` setup and when to run after bero bumps ([SMOKE_TESTS.md](SMOKE_TESTS.md)) |

Commit all three required paths (and optional `smoke-tests.mdc` for bero consumers) to the application repository so all contributors get the same guardrails.

## Local vs remote environments

| Typical env | `docker_host` | Agent default |
|-------------|---------------|---------------|
| `dev` | (none) | Local Docker — safe for routine dev commands |
| `full` | (none) | Local prod-like stack |
| `staging`, `prod` | `ssh://…` | **Remote** — confirm with user before any `dk` command |

Check `docker/envs/<env>/info.yaml` for `docker_host`. Custom env names follow the same rule: if `docker_host` is set, treat as remote.

## Secrets: what `.cursorignore` excludes

- `docker/envs/**/credentials.yaml` (including SOPS ciphertext)
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
- `uv run native fetch db`, `uv run native fetch media`
- `ssh` to hosts named in `info.yaml`

## Dev environments

Some projects put low-risk dev defaults in `docker/envs/dev/info.yaml`. OAuth client secrets should live in optional dev `credentials.yaml` or encrypted files when possible. Agents can still read plaintext `info.yaml` — keep production-like secrets out of it.

## Related docs

- [README.md](../README.md) — install and `docker/envs/` layout
- [SMOKE_TESTS.md](SMOKE_TESTS.md) — `test smoke` contract and authoring tests
- Consumer `docker/envs/README.md` — per-env credentials bootstrap
