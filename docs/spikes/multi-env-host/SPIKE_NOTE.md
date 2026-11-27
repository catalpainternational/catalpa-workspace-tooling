# Several envs on one host: the spike's design note (27–28 Nov 2026, kept as written)

> The built feature is described in [../../MULTI_ENV_HOST.md](../../MULTI_ENV_HOST.md). This note, its
> evidence and its demo are the spike that led to it (Forgejo branch `docs/multi-env-host-spike`).
> Some of its proposals changed: `fcos.subuid_start`/`fcos.local_port` became a slot in host.yaml.

Status: spike, 2026-11-27 (Vikunja #2383). Nothing here is built for real yet. The evidence
(commands and output from a local Fedora CoreOS VM) is in
[spikes/multi-env-host/EVIDENCE.md](EVIDENCE.md); the demo project, the
host-Butane merge and the host edge are beside it.

## The question

Josh, 27 Nov: "Multiple envs per droplet should become a catalpa-workspace-tooling feature.
Spike it in a VM. Evaluate how to resolve having 3.6 / 3.7 / 3.8 unit templates
simultaneously on a server (version controlled template files?)".

Today a rendered host runs one env: one droplet, one stack account (`fcos.user`, default
`deploy`), one edge taking ports 80 and 443 (`fcos/quadlets.py`: "one project per host, as one
account"). Partisipa's staging is about to run three releases on one server (Vikunja #2382,
on containerwharf), and each release ships its own unit templates
(`infra/quadlets/units/partisipa-backend@.container` and the rest, plus `units-version`).

## Recommendation: one Linux account per env, and a host edge

Give every env on a host **its own account** (`env-a`, `env-b`, …: own uid, own range of
subordinate IDs), and put one **host edge** in front of them in an account of its own.

- An account has its own quadlet folder (`/etc/containers/systemd/users/<uid>/`), its own
  systemd user manager, Podman secrets, networks, container names and data folder. Two envs can
  ship a file with the same name, at different versions, and never see each other's. That is
  how the 3.6 / 3.7 / 3.8 question goes away: there is no shared template to fight over.
- The host edge takes 80 and 443 (socket activation, as an env's edge does today), ends TLS
  for every name on the host, and sends each name to its env on the host's loopback
  (`127.0.0.1:<port>`). The env's own Caddy keeps its routes, takes plain HTTP on that port
  (also socket-activated) and trusts the edge's `X-Forwarded-For`.
- dk keeps doing what it does per env: `render`, `check`, `secrets push`, bare `dk <env>`,
  `db restore`, `exec` all worked unchanged against an env's own account. What is new is a
  **host level**: the files every env would otherwise fight over, the edge, and creating and
  destroying the droplet.

Proved in the VM (measured, EVIDENCE.md): three envs of one demo project rendered by dk and
deployed with bare `dk <env>` on one host, each answering on its own name through the host
edge; restoring env b's database restarted only env b's caddy and web; upgrading env a and
rolling it back (a `git revert` of its render) restarted only env a's web; removing env c by
hand touched neither a nor b. The visitor's address reached the app through both hops.
Same-named templates at units versions 9 and 10 ran side by side in two accounts, and one
account went 9 → 10 → 9 while the other stayed on 10, never restarted.

## The options for different unit versions on one host

Measured in the VM with raw Podman 6.1.1 (EVIDENCE.md part 1), using a template whose two
versions differ in image and settings.

| | What systemd and quadlet allow | Rollback | Two envs on different versions, upgraded one at a time | Under version control |
|---|---|---|---|---|
| **(a)** version-named templates (`partisipa-v9-backend@`) in one account | Works: `demo-web-v9@s36` and `demo-web-v10@s38` ran side by side | Point the env back at the older name; both sets must stay installed | Yes, but every name carries the version: container names, units, timers, scripts, monitoring | The project renames its whole set at every bump; the host holds every version still in use |
| **(b)** each env's units in its own application folder, **one account** | Installs without complaint, but the generator **silently picks one** `demo-web@.container` (it ran demo36's v9 while `podman quadlet list` showed demo38's v10) | — | No: file names must be unique per account | — |
| **(c)** shared templates plus per-instance drop-ins | Works only with an instance file beside the template (`demo-web@s38.container` + `.container.d/`); then the drop-in changed image and settings for that instance only. Partisipa's installer already does this for its sites | Remove the drop-in | Only for differences a drop-in can say: a new container, pod or timer can't be one | The newest set is installed; older versions live as drop-ins, written by hand per difference |
| **(d)** = (b) with **one account per env** (recommended) | Works: same names, different versions, per account | `git revert` the env's committed render and deploy it; or render an older tag | Yes: each account installs only its own env | Each env's render is committed under `docker/envs/<env>/generated/fcos/`, as today |

Why (d): it is the only option where a release's units are installed exactly as that release
shipped them, with no renaming and no hand-written differences, and it reuses the per-env
render and check that dk already has.

### Where the template files live, and how a host's installed set is recorded

Josh asked whether template files should be version-controlled. With (d) they are, twice:

1. **The project's own units**, in the project at each tag (Partisipa's `infra/quadlets/`),
   as now. They are the source.
2. **What each env runs**, rendered from that env's tag and committed in the deploying repo
   under `docker/envs/<env>/generated/fcos/` (for an app env: `…/generated/fcos/app/`, from the
   units image of the 23 Nov plan). `dk <env> check` compares the commit with a fresh render;
   bare `dk <env>` applies it and refuses when they differ. The render's `manifest.json` names
   the account, uid, subordinate IDs, network and every file's place on the host.
3. **On the host**, dk already writes each env's deploy record under its settings folder;
   per env that is now `/etc/<project>/<env>/` (spike change, below). So "what does env b run"
   is answered by the commit that produced its record, and a rollback is that commit's parent.

## What has to be host-level (found by merging three envs' `host.bu`)

`merge_host_bu.py` merges each env's generated `host.bu` and reports any path two envs write
differently. That list is what a host render must own (measured):

- `/etc/subuid`, `/etc/subgid`: one line per account (each env now needs its own start,
  `fcos.subuid_start`).
- `/etc/ssh/sshd_config.d/10-hardening.conf`: `AllowUsers` must name every account.
- `/etc/systemd/user.conf.d/10-registry-auth.conf`: one file for every user manager, but it
  names one account's home. Moved to each account's `~/.config/environment.d/`.
- core's `~/.ssh/authorized_keys.d/dk`: the same keys from every env, but each env's apply
  rewrites it (header comment), so deploys of different envs keep asking to change it.
- Host-wide settings each env repeats today: hostname, time zone, Zincati's window, the
  unprivileged-ports sysctl, the network-online fix, Zabbix (one agent per host; not tested).
- Each env's files embedded by relative path (`local: quadlets/caddy.container`): the same
  paths in every env.

And one collision that wasn't in `host.bu`: every env of a project put its settings files and
deploy record in `/etc/<project>/` (`ops.config_dir`), so env b's deploy would overwrite env
a's. Spike fix: `/etc/<project>/<env>/` when the env is behind a host edge.

The merged Ignition for three small envs and the edge was 30,484 bytes, against
DigitalOcean's 64 KiB user-data limit. A real host Ignition should carry only the host and the
accounts, and let each env arrive by `dk <env>` afterwards, which already works (envs b and c
were applied over SSH).

## What the spike changed in dk (branch `docs/multi-env-host-spike`)

Two `fcos:` keys, default off, so every existing render is unchanged:

- `fcos.subuid_start`: where the env's account's subordinate IDs start on the host. Used in
  the manifest, bind-volume owners and `host.bu`.
- `fcos.local_port`: the env sits behind the host edge. Its edge socket is
  `ListenStream=127.0.0.1:<port>` instead of 80/443; its Caddy has one plain-HTTP server on
  that socket with `trusted_proxies` set to loopback; its settings folder is
  `/etc/<project>/<env>/`.

Not built: the host level itself (the merge script and the edge files are hand-made
stand-ins), `dk <env> remove`, assigning ports and ID ranges, and tests for the two keys.

## What a real feature looks like

Revised after the literature review ([spikes/multi-env-host/LITERATURE.md](LITERATURE.md)).

- **Host config** in the deploying repo, `docker/hosts/<host>/host.yaml`: the droplet
  (`digitalocean:`), admin keys, update window, monitoring, and the envs (or colours) on it.
  - Each one has a **slot number that is never reused**. Its account, uid, ID range and
    loopback ports follow from the slot, so nobody picks numbers by hand and a removed env's
    numbers never come back.
  - `host.yaml` renders two ways: `host.bu` for a new droplet, and an idempotent apply for a
    running one. The apply only changes files: accounts exist only from first boot (below),
    so a new env means a new server.
  - A fresh `host create` must always give the same host as the applies did. Ignition runs
    only at first boot, and Fedora CoreOS's own answer to change is to replace the machine.
  - `dk host check` reads the live accounts, ID ranges, AllowUsers and linger, not just the
    commit.
- **`dk host <host> render | check | create | destroy | apply`**: renders `host.bu` (accounts,
  shared files above) and the edge (`caddy.json` from the envs' `site_origin` and
  `redirect_origins`), committed under `docker/hosts/<host>/generated/`. `host create` and
  `host destroy` move here from the env: on a shared host, destroying one env's droplet
  destroys every env.
- **Envs** say which host they live on (`host: <host>` in `info.yaml`) instead of carrying
  `fcos:` host keys. Everything per env stays as it is.
- **No account is added or removed on a running server** (Josh, 28 Nov; see "Simplified"
  below). `dk <env> stop` stops an env's units and takes its route off the edge; its account
  and data stay until the server is replaced. Change the edge by reloading Caddy, not
  restarting it. The spike's removal order (disable linger, terminate the session, `userdel`,
  which also removes the account's ID ranges: measured) only matters for clearing up by
  hand.
- **Loopback is shared by every account.** A container given pasta's `--map-host-loopback` can
  reach every service on it, so each port should accept only its own site's accounts. The
  proposal is an nftables rule matching the connecting uid, still to test. Backends trust
  `X-Forwarded-For` only from 127.0.0.1.
- **Quadlet folders:** `/etc/containers/systemd/users/<uid>/` is a real numeric folder, never a
  symlink or a username (both leak units to every account). Make it `root:<gid> 0750` so
  `podman quadlet list` stops showing other accounts' units (to test). dk installs timers
  itself and warns about file types Quadlet ignores.
- **Deploys of blue/green sites:**
  - Trial the next release's migrations on a restored copy before the outage, with a time
    budget.
  - Migrate as its own step, with a per-session `statement_timeout`.
  - Fail fast on migrate's exit code, a stopped container or a passing health check; the
    timeout is only a backstop.
  - `pg_terminate_backend` before the freeze.
  - Keep a dump per deploy.
  - Rollback states its window and asks for confirmation that writes since the deploy will be
    lost.
  - Give Caddy a finite `grace_period`.
- **Backups:** one pgBackRest stanza and one restic repository per env, each with its own
  credentials. For a local S3 store, use versitygw, which keeps objects as plain files that
  restic backs up directly. MinIO is archived.
- **Monitoring:** one Zabbix agent per host needs a way into each account: a Podman API per
  account on loopback, or narrow sudo rules. Not chosen yet.

Measured cost per account: 51–59 MB for a running env's processes (Caddy, whoami, Postgres,
conmon, its systemd and pasta), 27 MB with everything stopped, 78 MB for the edge.

## How this changes the 23 Nov app-envs plan

(`~/sessions/plan-2026-11-24-partisipa-staging-on-dk.md`, extended 27 Nov to Nextgen.)

- **An app env is one account.** Partisipa's release `partisipa-staging-37` (its blue and green
  copies and its database) runs in one account; 36 and 38 in two others. Partisipa's own
  "newest units-version wins" rule then applies inside one env only (between its blue and
  green), never across releases.
- **The units image renders per env, from that env's tag**, into that account's own application
  folder: option (d). Nothing about template names changes in Partisipa.
- **The front changes.** The plan routed dk's edge to `partisipa-partisipa-staging-site:80`
  over a shared `edge-net` network. Accounts can't share a Podman network, so the app's front
  must listen on the env's loopback port instead, which dk can add as an instance drop-in. To
  check: whether a published port keeps the visitor's address, or the front needs socket
  activation as dk's own Caddy has (inferred, not tested).
- **Host steps move to the host level**: `digitalocean.context`, swap and `host create` belong
  to `docker/hosts/<host>/`, not to an env.
- The order is unchanged: Partisipa's units split first, then app envs, now built on the
  account-per-env layout rather than one `deploy` account.

## Blue and green in separate accounts (tried in the VM, 27 Nov)

Josh asked whether each colour of a blue/green site should get its own account, so that each
colour manages its own templates and env files. His note: uploads are mostly not a shared
folder (Partisipa's media is on S3, and others can run a local S3 server). So the test was
three accounts per site:
- `site`: Postgres, a SeaweedFS S3 store and the site's front;
- `blue` and `green`: one colour each;
- plus the host edge.

The commands and output are in
[spikes/multi-env-host/colours/EVIDENCE.md](colours/EVIDENCE.md), and
the units and the deploy script are beside it.

**What worked (measured):**
- **Adding accounts to a running host.** Three accounts took 1.5 s with a 20-line script
  (`useradd`, a pinned subordinate range, SSH, `AllowUsers`, linger). dk doesn't do this yet;
  it is the host-level apply above.
- **Each colour runs exactly its release's units.** blue ran units version 10 while green
  ran 9. green went from 11 back to 9 and lost 11's extra job and timer. The units use plain
  names, with no `@` instances, no instance files and no "newest wins". The only per-colour
  file is the account's own drop-in, with the colour and its port.
- **The deploy shape Partisipa uses works across accounts:**
  1. install the idle colour;
  2. stop the live one;
  3. freeze the database;
  4. start the new colour;
  5. switch the front.

  The visitor saw about 2 s of errors.
- **A broken release puts itself back.** Its migration dropped a column, then failed. The
  deploy restored the frozen database and went back to the old colour, with nothing lost.
- **Rollback works.** It returns to the old colour and the database from before the deploy.
  As with Partisipa today, files uploaded in between stay in S3.
- **Uploads survive a switch, because they are in S3.** The visitor's address reached the app
  through the edge and the site's front.

**What a real feature must handle:**
- **Reaching another account's port.** Podman's `host.containers.internal` can't reach a port
  another account published on the host's loopback. Every unit that talks across accounts
  needs `Network=pasta:--map-host-loopback,<address>`, which dk can render.
- **The site's database and S3 store are reachable from every account on the host,**
  protected only by their passwords. Today the database sits on a network nothing else can
  reach.
- **Timers.** Quadlet ignores `.timer` files without a word. They go in the account's systemd
  folder, and the deploy removes the previous release's.
- **No fixed paths in `/tmp`, which every account shares.** The spike's first try installed
  the other colour's leftover files.
- **The orchestration.** A deploy talks to three accounts (21 SSH call sites in the script).
  Partisipa's own `deploy-site`, `switch-site`, `snapshot-db` and `rollback-site` assume one
  account, so this logic moves into dk. The app-envs plan already ports containerwharf's
  site deploy into dk (`fcos/site_deploy.py`), so this is a change of target more than new
  work.
- **A failed deploy must not use up the rollback point.** It did in the spike. Keep a dump
  per deploy, as Partisipa's `snapshots/` does.
- **The project's units split into a site set and a colour set.** For Partisipa:
  - site: database, Metabase, memcached, front, restore;
  - colour: backend, web, migrate and its jobs.

  The site set is shared, so its version still needs a rule. It changes rarely.

**Memory:** the site account used 123 MB, a live colour 103 MB, an idle colour 27 MB and the
edge 79 MB.

**Verdict, changed:** with media in S3, one account per colour works and is the cleaner
layout. Each release's units and env files are installed exactly as shipped, and the
version-mixing machinery goes away. The cost is a units split in Partisipa (and later Nextgen)
and the blue/green orchestration living in dk, which the app-envs plan already moves there.
Whether to build app envs this way, instead of one account per release, is Josh's decision.

### Simplified: every account exists from the server's first boot (Josh, 28 Nov)

**Accounts and their names:**
- `core` stays the admin account (passwordless sudo, `fcos.admin_keys`). No app ever runs
  there; dk already refuses `fcos.user: core`.
- A plain env, unless specified otherwise, runs in one account, `deploy`. This is dk's
  existing default.
- A blue/green site has three accounts: `site` (database, store, Metabase, memcached, front),
  `blue` and `green`.
- When one server holds several sites, each site's accounts take its name as a prefix:
  `s37-site`, `s37-blue`, `s37-green`.

Every account is declared in `host.yaml` before `dk host create`. Ignition makes it at first
boot, together with its slot, loopback ports, the shared folder and that folder's access list.

No dk command adds or removes an account on a running server, whether a plain env, a colour
or a site. Adding or removing one means a new server: create it, restore the data, move the
DNS. This is what Fedora CoreOS recommends anyway. The host-level apply only changes files.

**What this removes:**
- Creating or removing accounts over SSH (`useradd`, ID ranges, AllowUsers, linger,
  `userdel`) on a live server. `dk <env> remove` becomes "stop and keep the data until the
  server is replaced".
- Adding a colour to a shared folder that already holds files (a recursive `setfacl` over
  every upload), and removing one again.
- Slot reuse for colours: a colour's slot ends with its server.
- The shared-group option's restart problem. Group membership is set before any account's
  systemd first starts, so that option no longer needs a restart. Its remaining cost is that
  it changes the pod's user mapping, so the access list stays the recommendation.

**What stays the same:**
- Deploys, rollback and the switch between the two colours.
- Each colour installing its own release's units.
- Images from GHCR.

**How the shared folder is declared,** entirely in `host.bu` (inferred: still to test on a
fresh boot):
- A `file_contexts.local` line gives `/var/srv/<site>-media` the container label.
- A `tmpfiles.d` file holds:

  ```
  d  /var/srv/<site>-media  0770 root root -
  a+ /var/srv/<site>-media  -    -    -    -  u:<blue>:rwx,u:<green>:rwx,u:<site>:r-x,d:u:<blue>:rwx,d:u:<green>:rwx,d:u:<site>:r-x,d:m::rwx
  z  /var/srv/<site>-media  -    -    -    -
  ```

  `<blue>` and `<green>` are the colours' mapped container users, worked out from their slots.
  systemd-tmpfiles applies the lines at every boot, so nothing about them lives only on the
  server.

### Sharing a folder between colours, without S3 (tried, 28 Nov)

For apps whose uploads go to disk. The evidence is in
[EVIDENCE.md](EVIDENCE.md), part 4.

**Recommended: an access list on a host folder**, e.g. `/var/srv/<site>-media`, set by the host
apply.
- It names each colour's mapped container user, computed from the colour's slot (subuid
  start + container uid − 1), and the site account for backups.
- It includes default entries, so new files and folders inherit them.
- Mount it without `:z`, `:Z` or `:U`. The SELinux label comes from a `file_contexts.local`
  line plus `restorecon`: Fedora CoreOS has no `semanage`, and a `chcon` is lost at the next
  relabel.

Measured:
- blue and green read, write and delete each other's files.
- New folders inherit the access list.
- Another env's account can't open the folder.
- Nothing about how the containers run changes.

**Alternative: a shared group** (`root:<site>-media 2770`, with each colour a member and the
containers run with `UserNS=keep-id:uid=1000,gid=1000` and `GroupAdd=keep-groups`). Files on
the host then belong to the real accounts, which is easier to read. But:
- It changes the user mapping of every container in the colour's pod.
- An account's systemd only picks up a new group when restarted, which restarts that colour's
  containers (measured).

**For both:**
- Set Django's `FILE_UPLOAD_PERMISSIONS = 0o664`. With the default 0o644, the other colour
  can read and delete a file but not change it (measured).
- Don't set `FILE_UPLOAD_DIRECTORY_PERMISSIONS` below 0o775.

**Ruled out:** Podman's `:idmap`. A rootless account can only map IDs inside its own ranges
(measured).

### Partisipa's template units under dk, one account per colour

Templates stay as Partisipa ships them. Each account installs only its own set and starts one
instance:

| Account | Installs | Instance |
|---|---|---|
| `site` | db pod, postgres, memcached, metabase, db-from-production, site-router, the db and site targets | `@<site>` |
| `blue` | the copy set: pod, backend, web, migrate, collectstatic, post-migration-tasks, after-restore, image, target | `@<site>-blue` |
| `green` | the same copy set, at green's own release | `@<site>-green` |

**What dk does.**
1. The units image renders each account's files at the release tag.
2. dk commits them under `generated/fcos/app/{site,blue,green}/` and checks them like any
   render.
3. It installs them, putting timers in the account's systemd folder, and pushes each
   account's secrets.
4. It runs Partisipa's site deploy step by step in the right account: build in the idle
   colour, stop live, freeze in `site`, start the new colour (migrate first), switch in
   `site`, dump for rollback.

**What changes in Partisipa.**
- `shared-db` gets a cross-account form:
  - the pod reaches the site's database and memcached on loopback;
  - no stand-in postgres;
  - no media volume, since media is on S3.
- The database and front parts of `snapshot-db`, `switch-site`, `rollback-site` and
  `refresh-site-data` run in `site`, while dk starts and stops colours. `app.toml` says which
  account runs each script.
- `units-version` still governs workstations and the site set.

**Images from GHCR, not built on the server (Josh, 28 Nov).** Partisipa's CI already pushes
the backend and frontend to GHCR for every `v*` tag. The servers pull them in the idle
colour's account while the site still serves, and the committed render records each image's
digest. Three gaps first:
- **The staging frontend.** The server build bakes in staging settings (environment, Sentry
  address, release). CI's image has none. Either CI builds a `-staging` variant, or the
  frontend reads them at runtime.
- **The Postgres image isn't built in CI.** It comes from the units and runs only in `site`.
- **GHCR's clean-up keeps 10 versions.** Release tags must be exempt, or a rollback finds no
  image.

Each colour account logs in to GHCR with a read-only token through dk's existing registry
login. Each account still stores its own pulled images.

**To try.** A template-wide drop-in (`partisipa-backend@.container.d/`) for the per-colour
settings, instead of instance files.

## Open questions for Josh

- Host config in the project's repo (`docker/hosts/`), or somewhere hosts of several projects
  can share (Partisipa and Nextgen on one droplet)? The spike assumed one project per host.
- Should a removed env's data folder be kept by default?
- App envs: one account per colour (blue, green, and a site account for the database, S3 and
  front), as tried above, or one account per release? The first needs Partisipa's units split
  into a site set and a colour set. (Josh, 28 Nov: one account per colour, created only with
  the server.)
- ~~Should plain envs follow the same rule?~~ Yes (Josh, 28 Nov): no account is ever added on a
  running server. Names: `deploy` for a plain env, `site`/`blue`/`green` for a blue/green site.
