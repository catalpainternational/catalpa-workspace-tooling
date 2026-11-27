# Several envs on one server

Several envs, of one project or of several, can share one Fedora CoreOS server. Each env runs
in a Linux account of its own, and a blue/green site runs in three: one for what both colours
share, and one per colour. One shared edge ends TLS for every name on the server.

How to use it: [SHARED_HOST.md](SHARED_HOST.md). Proof on a VM: [examples/shared-host/EVIDENCE.md](examples/shared-host/EVIDENCE.md).
How we got here: the spike's [design note](spikes/multi-env-host/SPIKE_NOTE.md), its
[evidence](spikes/multi-env-host/EVIDENCE.md), the [blue/green-in-accounts trial](spikes/multi-env-host/colours/EVIDENCE.md)
and the [literature review](spikes/multi-env-host/LITERATURE.md).

## The design

- **One account per env, or per site and colour.** An account has its own uid, range of
  subordinate IDs, quadlet folder, systemd, Podman secrets, images and data folder. Two
  accounts can install units with the same file names at different versions and never see
  each other's. One account with two app folders isn't safe: Quadlet silently runs one of
  two same-named templates (measured in the spike, and documented upstream).
- **A server is described by its own folder,** `host.yaml` in a folder of its own: a
  project's `docker/hosts/<host>/`, or a hosts repo several projects check out beside them.
  An env says which server it is on with `host: <path to that folder>` in its info.yaml.
  `dk host <folder> render|check|ignition` needs no project.
- **Every account exists from the server's first boot, and only then** (Josh, 28 Nov). No
  dk command adds an account to a running server. A new env, or one taken away, means a new
  server. This is Fedora CoreOS's own model: Ignition runs once.
- **Slots, not numbers.** host.yaml gives each account a slot (1-30), never reused. The slot
  fixes the uid (1000 + slot), the subordinate IDs, and ten loopback ports
  (20000 + 10 × slot on).
- **The shared edge** (account `edge`) takes 80 and 443 by socket activation and sends each
  name to its account's first loopback port. A plain env's own proxy listens there in plain
  HTTP and trusts the edge's `X-Forwarded-For`; the visitor's address survives both hops.
- **Accounts reach each other only through the loopback ports their slot gives them**
  (pasta's `--map-host-loopback`; Podman's `host.containers.internal` reaches only the
  server's outside address, by design). A firewall rule lets an account's ports take
  connections only from its own env's accounts and the edge.
- **A blue/green site runs the units the project ships.** Each account installs one version:
  `site` (the database, the front, the hooks' scripts), and `blue` and `green` each at their
  own version, from a git ref or a folder. dk adds one drop-in per unit that publishes a
  port. The site's own scripts freeze, thaw and switch, as Partisipa's do. dk drives each
  step over SSH as each account, in the order of Partisipa's own `deploy-site`.
- **Settings travel as secrets per account,** from the env's credentials.yaml under
  `site:`, `blue:` and `green:`, as containerwharf does. dk creates them; the app's units
  mount them.
- **Images come from a registry, not a build on the server** (Josh, 28 Nov: Partisipa's
  CI already pushes to GHCR).

Names (Josh, 28 Nov): `core` is the admin account and runs no app; `deploy` is a plain env's
account unless host.yaml says otherwise; a blue/green site's accounts are `site`, `blue` and
`green`, prefixed with the site when a server holds several (`s37-site`).

## What it fixes from a real several-app server

From the rebuild of Partisipa staging as three apps on one 8 GB server (27–28 Nov 2026,
Vikunja #2382), done with containerwharf. Each item is marked "(measured: how)" or "(from
notes)". The verdicts:

- **Removed:** the one-account-per-env design makes it impossible.
- **Fixed here:** this change handles it.
- **containerwharf:** it stays containerwharf's to fix.
- **Process:** about how people and sessions work.

| # | What happened | Verdict | Why |
|---|---|---|---|
| 1 | Saving the TLS certificates before a rebuild skipped every name that belonged to a blue/green site, and reported "none to save" (measured: command output; Vikunja #2384) | containerwharf | dk's edge is per server and its names come from host.yaml, but this change does not keep certificates across a rebuild |
| 2 | `provision-stack --replace` re-delivered onto the running server instead of rebuilding it, so the size and image stayed the same (measured: same droplet id before and after; Vikunja #2385) | containerwharf | dk's server creation isn't part of this change |
| 3 | One server keeps one set of units for every app of a project: the newest installed wins, so three Partisipa versions had to share one units commit (measured: install-units output) | Removed | Each env, and each colour, is its own account with its own quadlet folder, so each installs its own units version. Proved on the VM with two versions of same-named templates |
| 4 | Memory: three apps used 5.7–5.8 GB of 8 GB, with no limit on any app except Metabase (measured: free -m, podman stats) | Stays | Separate accounts don't cap memory. Sizing and limits are still each project's call |
| 5 | The laptop's DNS cache held "no such name" for a new address, so a working site looked down (measured: curl with `--resolve` and `dig` against the authoritative server) | Process | Check a new name with `--resolve` or the authoritative server until caches expire |
| 6 | Production settings refuse to start without `MB_DB_CONNECTION_URI`, even on an app with no Metabase; the first deploy of a plain app failed (measured: journal) | Stays | Partisipa's settings, not the server's. The deploy contract does not list required settings keys |
| 7 | A plain app and a blue/green copy need different database, cache and Metabase addresses, which were edited by hand into 10 new vault keys (measured) | Fixed here | Each colour's settings are its own secret, pushed per account from credentials.yaml, and the site's ports are fixed by slot, so the addresses are known in advance |
| 8 | A blue/green site splits its settings between the site and each copy, and a plain app has a third shape (measured: fleet.yaml, Partisipa app.toml) | Fixed here | app.toml says which units and secrets are the site's and which are a colour's, and dk pushes each to its own account |
| 9 | Only a blue/green site can limit which tags it deploys; a plain app deploys whatever ref is pinned (measured: containerwharf schema) | Stays | Not in scope here |
| 10 | The DNS change was approved and run in another session's window, because the working session could not reach its user (measured) | Process | Approvals follow the person, not the session |
| 11 | The rebuild removed an SSH certificate-authority line added by hand to the server's login keys (measured: the new server's `~/.ssh`) | Fixed here | Accounts and their login keys exist only from first boot; host.yaml takes `cert-authority` lines, so one comes back with every rebuild (measured: `test_a_certificate_authority_line_is_kept_through_a_rebuild`) |
| 12 | Servers booted Fedora CoreOS `testing` because `stable` lacked Podman 6.1; stable has it now (measured: stable's package list) | containerwharf | Done there (containerwharf#442) |
| 13 | A prepared release-candidate commit sat on another session's local branch, found by chance (measured) | Process | Prepared release work goes on a pushed branch and its card |
| 14 | Partisipa's docs named the one old staging site in nine places, and nothing checks names in docs (measured: review rounds) | Process | Fixed in partisipa-import; a name check in docs is that repo's call |
| 15 | Switching on the 3.8 streams took about 45 minutes for 32,300 submissions (measured: log timestamps) | Stays | Partisipa's own commands |
| 16 | A deploy's log stays empty until it ends unless Python is unbuffered (measured) | Stays | A tooling habit; dk's deploy runs over SSH and prints as it goes |
| 17 | containerwharf's deploy set the database aside while the old copy's backend was still stopping: two of its connections were cut at 11:57:07.7, and it stopped at 11:57:09.7 (measured: staging's journal for the rc.2 deploy). Found by dk's VM run; Vikunja #2389 | containerwharf | dk stops the colour's target and every unit it wants in one call, which waits for all of them, as Partisipa's own `release-down` does; containerwharf's site deploy should too |

## What Partisipa's units need to run this way

Read from partisipa-import's `origin/develop` `infra/quadlets` on 28 Nov 2026 (measured:
read), unless marked otherwise. This change touches no Partisipa code; this is the list for
whoever moves Partisipa onto it. The test app `tests/fixtures/fcos/bluegreen-app` has the
same shape in miniature.

- **The split already exists.** The site set (`partisipa-site@`, `partisipa-db@`: the
  router, postgres, memcached, Metabase) goes to the `site` account, and the copy set
  (`partisipa@<copy>`: web, backend, migrate, post-migration tasks, collectstatic) to
  `blue` and `green`.
- **The pods keep `Network=edge-net.network`.** dk's drop-in clears it and puts in the
  host-loopback network (measured: an empty `Network=` in a drop-in resets the list).
- **The site's internal network becomes loopback ports.** app.toml lists them under
  `[ports]`: `edge` (the router), `colour` (a copy's front), and `db`, `memcached` and
  `metabase` (the site's). The router reaches Metabase that way too: the router's pod and
  the database pod are two pods of the site account, joined today by install-units' internal
  network, which dk's drop-in replaces.
- **install-units isn't run on a server.** dk installs the units itself, so install-units'
  server jobs aren't done: the shared-db network and its drop-ins
  (`partisipa-db@<site>.pod.d/shared-db.conf`, `partisipa@<copy>.pod.d/shared-db.conf`) and
  the edge route. Running install-units in an account as well would give a pod two
  `Network=` sources.
- **Each copy's settings name those ports.** `DATABASE_URL`, `MB_DB_CONNECTION_URI`,
  `METABASE_INTERNAL_URL` and `MEMCACHED_LOCATION` name `partisipa-<site>-db` today and
  would name `169.254.1.3:<port>`.
- **The secrets are already named per instance** (`partisipa-<copy>-dotenv`,
  `partisipa-<site>-db-postgres-password`…); dk creates them in the right account.
- **The hooks map to existing scripts:**

  | dk hook | Partisipa script | Change needed |
  |---|---|---|
  | frozen | `snapshot-db <site> frozen` | none |
  | freeze | `snapshot-db <site> freeze` | none: SQL in the site's postgres |
  | thaw | `snapshot-db <site> thaw` | none |
  | done | `snapshot-db <site> take-frozen` | none: it prints the dump's path |
  | switch | `switch-site <site> <colour>` | its health check (`podman exec` into the copy) and its upstream (`partisipa-<copy>:80`) must use the copy's loopback port, passed as an argument; its Metabase upstream (`partisipa-<site>-db:3000`) likewise |
  | rollback | `snapshot-db <site> restore <snapshot>` | none: rollback-site's own steps (stop both copies, thaw when there's no snapshot, start, switch) are what dk does |
  | keep | `snapshot-db <site> keep` | none: a dump of what a rollback throws away, kept in `snapshots/kept/` |

- **Images from GHCR:** today the site's postgres image is built with a copy and re-tagged
  for the site (measured: the rc.1 deploy log); the site account would pull its own.

## Not built yet

- `dk host <folder> create|destroy` on DigitalOcean: the server is made from the Ignition
  `dk host <folder> ignition` writes, by hand or by a later change.
- Backups and monitoring on a shared server: one pgBackRest stanza and one restic repository
  per env is the plan (literature review); one Zabbix agent needs a way into each account.
- Several names per account work (host.yaml `names:` is a list), so Partisipa's
  `metabase.<name>` and `www.<name>` go to the site account with its own name.
- `logs`, `ps` and `exec` for a blue/green site's accounts: use `ssh <account>@<server>`.
- A deploy trigger from a tag, and a trial run of the next release's migrations before the
  outage (both in the literature review).
- A lock left by a dk that was killed (`~/.local/state/dk/<env>/lock` in the site account)
  isn't detected: remove it by hand.
- Every account takes the same `ssh_authorized_keys`, so whoever holds dk's key can log in
  to any env's account. The loopback firewall separates containers, not people.
- app.toml is trusted: dk installs its units as root (their names can't leave the account's
  folder) and runs its hooks as the site account.
- Keeping a dump per deploy: as in Partisipa today, a deploy that fails and puts itself
  back clears the rollback record, so `rollback` then has nothing to go back to.
