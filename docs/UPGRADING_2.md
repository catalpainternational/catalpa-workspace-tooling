# Upgrading a project to tooling 2.0

What a project's maintainer changes when moving a project from tooling 1.x to 2.0. Work through it on
a branch (the M4 test branches do exactly this), run the checks at the end, then merge.

Most of 2.0 is new commands that change nothing until you use them (rendered hosts, `host create`,
`restore`). The parts below are the ones that **stop or change something you already do**.

## 1. Pin the tooling

In `pyproject.toml`, under `[tool.uv.sources]`:

```toml
# while 2.0 is being tested
catalpa-workspace-tooling = { git = "https://github.com/catalpainternational/catalpa-workspace-tooling", branch = "dev-2.0" }
# once released
catalpa-workspace-tooling = { git = "https://github.com/catalpainternational/catalpa-workspace-tooling", tag = "v2.0.0" }
```

Drop any `==1.x` version on the dependency itself, then `uv lock --upgrade-package
catalpa-workspace-tooling` and `uv sync`. Commit `uv.lock`.

**From a pin older than 1.7** (catalpa_bero, jid, ncd and tvi are on 1.3.3): read the `### Changed`
and `### Removed` sections of [CHANGELOG.md](../CHANGELOG.md) from your version up to 1.7.0 as
well. Most of 1.4–1.7 is fixes. The ones you might notice:

- **`db pgrestore` is gone (1.5.0).** `dk <env> db pgrestore` becomes `dk <env> db restore --dumps`
  (the configured dumps, as before), and `db pgrestore --file X` becomes `db restore --file X`.
  `git grep -n pgrestore` finds them, typically in the README's quick start.
- **The agent files moved into the package** (1.6.1): `uv run setup-agents` installs them (section
  4). A project that copied them by hand before has them without the managed marker, so
  `setup-agents --status` says "owned by the project". If they're plain copies, `uv run
  setup-agents --force` takes them over; keep any the project changed and update those by hand.
- **A `--yes` after `--` now goes to the program, not to dk** (1.6.2):
  `db restore -- --type=immediate --yes` → put `--yes` before `--`.
- **Every `ops:` key is optional** (1.7.0).

## 2. Say which envs back up and which restore (breaking)

Until 1.x, an env backed up when its `credentials.yaml` had `pgbr_s3_write_*` / `restic_write_*`,
and restored in READ mode when it had `pgbr_s3_read_*` / `restic_read_*`. In 2.0 `info.yaml` says
so, and **credentials without their setting stop every `dk <env>` command**: an env upgraded
without the setting can't quietly stop backing up.

```yaml
# docker/envs/prod/info.yaml: the env that backs itself up
backups: [db, files]   # db = pgBackRest, files = restic; list only what it backs up

# docker/envs/staging/info.yaml, and dev, full … with read credentials
restore_from: prod     # restores prod's backups (the kinds prod lists under backups)
```

To find which envs need what, look at the key names (no need to decrypt):

```bash
for f in docker/envs/*/credentials.yaml; do
  echo "$f: $(grep -oE '^(pgbr_s3|restic)_(write|read)' "$f" | sort -u | tr '\n' ' ')"
done
```

- `*_write` → `backups:` listing `db` (pgBackRest) and/or `files` (restic). Old flat
  `restic_repository` / `restic_password` count as restic write credentials.
- `*_read` → `restore_from: <the env those keys read>`.
- Neither → nothing to add. Such an env now refuses `db backup`, `files backup`,
  `install-systemd` and the rest, rather than offering to create a bucket.
- Credentials an env doesn't actually use (read keys nothing restores from, a write key left from
  an old setup) → remove them rather than add a setting, one key at a time:
  `sops unset docker/envs/<env>/credentials.yaml '["pgbr_s3_read_key"]'`, and so on for each
  `pgbr_s3_read_*` / `restic_read_*` (or `*_write_*`) key. Delete the Spaces key itself too if
  nothing else uses it.

An env can now get its own read key instead of sharing a hand-made one: remove its `*_read_*`
credentials, then `dk <env> secrets read-key` (see
[README_PGBACKREST.md](../README_PGBACKREST.md#which-envs-back-up-and-which-restore)). Not needed to
upgrade; the old shared keys keep working.

## 3. Compose verbs go through `dk <env> compose` (breaking)

`dk <env> up -d`, `dk <env> down`, `dk <env> config`, `dk <env> build`, … no longer reach
docker compose; they're refused with a hint. What stays:

| 1.x | 2.0 |
|---|---|
| `dk dev up -d` | `dk dev` (bare: still `up -d`) or `dk dev compose up -d` |
| `dk dev down` | `dk dev compose down` |
| `dk dev config`, `build`, `pull`, `run …` | `dk dev compose config`, … |
| `dk dev ps`, `logs`, `exec`, `start`, `stop`, `restart` | unchanged |

Find what to change:

```bash
git grep -nE 'dk [a-z_<>${}-]+ (up|down|config|build|pull|run|create|rm|kill|pause|top|images|port|cp)([^a-z_-]|$)' \
  -- ':!uv.lock' ':!CHANGELOG*' | grep -vE 'dk (worktree|proxy) '
```

READMEs, Makefiles, CI workflows, scripts, the project's own agent skills (`run-app` and the like),
`.claude/settings.json` permission rules (`Bash(uv run dk dev up:*)`), comments in compose files
and `info.yaml`, and the project's own tests that assert on such a hint usually have some.
`dk proxy …` and `dk worktree …` aren't env commands and stay as they are.

## 4. Regenerate the editor and agent files

```bash
uv run setup-vscode      # VS Code tasks (generator 9): `dk dev`, `dk dev compose down`
uv run setup-agents      # .cursor/rules, the dk-stack skill, .cursorignore
uv run setup-agents --status   # should now say everything is current
```

Commit what they change. If the project customised a generated file and took it over, update it by
hand along the lines of section 3.

## 5. Other changes you may notice

- **`db backup` defaults to `incr`.** `dk prod db backup` with no type now takes an incremental
  (a full one when the repository has none). `full` / `diff` still work; scripts passing a type are
  unaffected.
- **restic 0.18.1** (was 0.17.3) on every host. Same repository format; hosts pull the image on the
  next backup. A systemd env file that pins `RESTIC_IMAGE` keeps its pin.
- **`dk <env> host create` puts a cloud firewall on every new droplet** (`dk-<droplet>`): SSH, HTTP,
  HTTPS and ping in; DNS, HTTP(S), NTP, mail, git over SSH and Zabbix active checks (10051) out.
  Existing droplets aren't touched. If the project's Zabbix items are **passive** ("Zabbix agent",
  not "Zabbix agent (active)"), the server can't reach port 10050 through it: check before
  creating a compose host, or set `digitalocean.firewall` in `info.yaml` (`false`, or
  `{out_tcp: [...]}` for more outbound ports).
- **The Spaces write key dk creates is `<bucket>-<env>-write`** (was `<bucket>-write`). Existing
  keys and credentials are left alone.
- **`docker/credentials.yaml`** (project level, beside `envs/`) is new and optional. Rendered hosts
  read it; compose envs don't yet. It needs its own `.sops.yaml` creation rule.
- **`dk <env> restore`** restores the database and the media together, with one confirmation.

## 6. Check

```bash
uv run dk dev                          # starts as before
uv run dk dev ps
uv run dk dev compose down && uv run dk dev
uv run dk dev up -d                    # refused, with the hint: expected
uv run dk prod db info                 # read-only: an env with backups: and its write credentials
uv run dk staging --dry-run restore    # an env with restore_from: plans a READ-mode restore
uv run dk worktree create <slug>       # and `dk worktree remove <slug>`
uv run setup-agents --status
```

Any `dk <env>` command that stops with "has … write credentials but doesn't back up" or "has …
read credentials but …" points at section 2. (`dk <env> --dry-run db …` stops before dk reads the
credentials, so it doesn't show this; the commands above do.)
