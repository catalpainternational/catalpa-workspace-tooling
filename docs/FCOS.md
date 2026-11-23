# Fedora CoreOS: rootless quadlets rendered from compose

`dk <env> render` turns an environment's compose stack into the files a Fedora CoreOS
host needs to run it as rootless Podman quadlets. The files go in
`docker/envs/<env>/generated/fcos/`, and you commit them. `dk <env> check` compares the
committed files with a fresh render, so what reaches a host is what was reviewed.

This is #182 ("option B"). A render holds everything a new host needs, including its Butane
config (`host.bu`), backups and monitoring. `host ignition`, `host provision`, `secrets push`,
`diff` and `apply` put it on a host ([On the host](#on-the-host)). Until tooling 2.0 these were
`dk <env> fcos render|check|diff|apply|ignition|provision|secrets push`; that spelling still
works, with a note giving the new one. The design and the proof of concept
are in catalpa-site's `docker/envs/fcos-rootless/` (catalpa-site#1006).

## Principles

- **The compose file stays the description of the stack.** Plain `docker compose up` must keep
  working in the repository without `dk`, which matters when a project is handed over. Nothing
  here needs a compose or Caddyfile change that only works under `dk`.
- **Plain compose refuses to start without a secret.** Write each credential as
  `${KEY:?KEY must be set}`, never with a default. `check` enforces this: for each
  credential, it resolves the compose file without that credential and expects compose to
  refuse.
- **A render holds no secret, and it's the same whoever runs it.** It reads only the *names*
  in `credentials.yaml` (SOPS leaves the keys in the clear) and never decrypts. It ignores the
  shell's environment and the `dk` worktree overlay. Each credential becomes a Podman secret
  named after its key in lower case (`POSTGRES_PASSWORD` becomes `postgres_password`). A
  credential is never written to a settings file, because `podman inspect` shows settings-file
  values.

## info.yaml

```yaml
image_tag: v1.2.3              # required: the host pulls, it never builds
runtime: quadlet               # compose (default) | quadlet
monitoring: zabbix             # omitted or false (default): not monitored (see Monitoring)
digitalocean:                  # for `host create` / `host destroy`
  droplet_name: catalpa-site-prod
  image: "243427036"           # the account's Fedora CoreOS custom image ID
  size: s-2vcpu-4gb
  region: sgp1
  allow_destroy: false         # true only for a disposable env
storage:                       # shared with compose envs' storage settings
  data_volume:                 # omitted: /var/mnt/data on a 10 GiB <hostname>-data volume
    path: /var/mnt/data        # the data directory; each kept volume is a subdirectory
    digitalocean:              # omitted: the directory is on the root disk
      name: catalpa-site-prod-data   # default <hostname>-data
      size_gib: 10             # when `host create` makes it
      filesystem: xfs          # default xfs
fcos:                          # every key is optional
  user: deploy                 # the unprivileged account that runs the stack
  uid: 1001
  bind_volumes: [pgdata, django_media, caddy_data]
  id_ranges: {db: 10000}       # pin a group's ID range, by any service in the group
  auto_update: false           # podman auto-update (staging/test sites; prod deploys explicitly)
  drop_ports: [mailpit]        # services whose published ports are dropped, not refused
  caddyfile: /etc/caddy/Caddyfile   # the Caddyfile's path inside the edge image
  volume_owners: {pgdata: 999} # uid inside its range that owns a bind volume (default 0;
                               # the pgBackRest data volume 999, postgres)
  # The host (host.bu):
  admin_keys:                  # required: public keys for core (sudo, so root): admins only
    - ssh-ed25519 AAAA… an-admin
  ssh_authorized_keys:         # required: public keys for the account (dk, CI): no sudo
    - ssh-ed25519 AAAA… an-admin
    - ssh-ed25519 AAAA… ci-deploy
  hostname: catalpa-site-prod  # default <project>-<env>
  timezone: Asia/Dili          # default tooling.yaml digitalocean.timezone, else UTC
  update_window: {days: [Sun], start: "02:00", minutes: 60}   # when Zincati may reboot
  zabbix_image: docker.io/zabbix/zabbix-agent2:alpine-7.0-latest
```

`runtime` decides how a host is set up as well as how the stack runs. `quadlet` is Fedora CoreOS
with Ignition and `compose` (the default) is Ubuntu with cloud-init, and hosts are on
DigitalOcean. There are no `bootstrap:` or `provider:` settings, and dk refuses them, as it refuses
an `fcos:` block without `runtime: quadlet`. `host create` also refuses an image that isn't a
number: `digitalocean.image` in `tooling.yaml` is usually Ubuntu's slug, for compose envs, so a
quadlet env sets its own.

With `runtime: quadlet`, the compose commands are refused: they would find no stack on that host,
or would start a second one. The commands that do work are in
[dk's stack commands on a quadlet env](#dks-stack-commands-on-a-quadlet-env).

Images, settings, origins and credentials come from the keys `dk` already reads for compose
deploys. The `bind_volumes` default comes from `ops`: `ops.pgbackrest.data_volume`,
`ops.restic.data_volumes`, and `caddy_data`.

## What a render contains

| Path | Installed as | What |
|---|---|---|
| `quadlets/<service>.container` | `/etc/containers/systemd/users/<uid>/` (root-owned) | one per running service; `ContainerName` is the service name |
| `quadlets/<project>_default.network`, `quadlets/<volume>.volume` | same | compose's default network; volumes not kept on the data mount |
| `envfiles/<service>.env` | `/etc/<project>/` | settings without credentials |
| `systemd/<proxy>.socket` | `~<user>/.config/systemd/user/` | opens 80/443 for the edge |
| `caddy/caddy.json` | `~<user>/.config/<project>/` | the edge's config |
| `backup/`, `systemd/<…>.timer`, `quadlets/<…>restic-files-backup.container` | the account's config and units | backups, when the env has backup credentials |
| `quadlets/zabbix-agent2.container`, `quadlets/podman-ro-proxy.container`, `monitoring/` | `/etc/containers/systemd/` (system), the account's quadlets, `/etc/<project>/` | monitoring, when tooling.yaml has `ops.zabbix` |
| `host.bu` | compiled to Ignition | the host: users, data volume and directories, every file above, hardening |
| `manifest.json` | — | ID ranges and host uids, bind volumes and their owners, secrets, timers, the install map |

How compose maps to quadlets:

- **One-shots.** A service another service waits on with `service_completed_successfully`
  (migrate, collectstatic) becomes `Type=oneshot` with `PartOf=` the waiting service. It runs
  again whenever that service restarts, for example after a new image.
- **Health.** A service another waits on with `service_healthy` gets `Notify=healthy`.
- **Plain `depends_on`.** This becomes `Wants=` plus `After=`.
- **ID ranges.** Services that write the same volume, or run the same image, share an ID range
  (`UIDMap`/`GIDMap`, 5000 IDs each, from 10000 up). Nothing else shares a host uid with them
  or with the account. A read-only mount (`:ro`) doesn't join a group, so mount the app's
  volumes into Caddy read-only if you want Caddy to have its own range. The ranges recorded in
  the committed `manifest.json` stay where they are when services are added, because data on
  the host is owned by uids from those ranges.
- **SELinux labels.** A data-mount volume used by one container gets the private label `:Z`.
  One used by several gets the shared label `:z`.
- **Refused.** The converter refuses, naming every key: bind mounts, build-only services,
  networks other than the default, published ports outside the edge, a credential inside a
  longer value (such as a database URL), and keys it can't carry over.

## The edge: socket activation and caddy.json

Rootless `PublishPort` hides visitors' addresses: connections arrive from Caddy's own
container address. If Caddy trusts `private_ranges` as proxies, a forged `X-Forwarded-For`
then gets through too. So the account's systemd opens the ports in `<proxy>.socket` and hands
them to the container:

- fd 3 is TCP 80;
- fd 4 is TCP 443;
- fd 5 is UDP 443.

The render runs `caddy adapt` on the project's own Caddyfile inside the edge image, which
gives the same Caddy version and plugins. It then points each server's listeners at those fds
and adds the HTTP-to-HTTPS redirect explicitly.

- **Settings.** `{$VAR}` is filled in at adapt time from the edge's settings. A credential
  reaches the adapt step only as a marker, and if that marker ends up in `caddy.json` the
  render stops. Read secrets at runtime with `{env.VAR}`, as swlt's site gate does.
- **Entrypoint.** If the edge image has an entrypoint that wraps Caddy, it is kept, and the
  unit passes `run --config /etc/caddy/caddy.json` to it.

Rendering needs `docker` locally: the CLI for `docker compose config`, and a daemon for the
edge image. Pulling an amd64 image on Apple Silicon takes a minute or two the first time.

## Backups

`backups: [db, files]` in `info.yaml` switches them on, kind by kind, as on compose envs; without
it the render has no backup units, archiving or timers, whatever credentials exist. The render
lists the required credentials as secrets even before they're set, so `render` → `host
provision` → `db configure stanza-create` runs in that order on a new env (`host provision`
names the missing ones and the command that makes them). They use the same keys as compose
deploys:

- `pgbr_s3_write_*` and `restic_write_*` in the env's `credentials.yaml`. When they're missing,
  `db configure stanza-create`, `db backup`, `files init` and `files backup` offer to create the
  Spaces bucket and a key, as on compose envs (README_PGBACKREST.md, "Auto-provision"). Then they
  push the changed Podman secrets to the host and restart `db`. `spaces:` in `info.yaml` gives
  the env a bucket of its own;
- `ops.pgbackrest` and `ops.restic` for what to back up;
- `ops.systemd_units` for which timers exist and which are enabled, by the names listed there.

Schedules come from `dk`'s own timer templates, so both runtimes back up at the same times, in
the host's time zone.

`db` runs with an init as PID 1 (`RunInit=true`) whenever pgBackRest is configured. Async
`archive-push` leaves a process behind. With postgres as PID 1, postgres inherits it, and when
that process exits non-zero (the repository unreachable, or its credentials wrong) postgres
takes it for a crashed backend and restarts in recovery mode, about every ten seconds. Compose
deploys have the same exposure unless the db service sets `init: true`.

A render never decrypts, so the repository settings can't go into pgBackRest's config as they
do on compose hosts. Instead, every `pgbr_s3_write_*` key reaches `db` as a Podman secret in
the variable pgBackRest reads: `PGBACKREST_REPO1_S3_BUCKET`, `PGBACKREST_REPO1_PATH`,
`PGBACKREST_STANZA`, and so on. Every `restic_write_*` key reaches the restic container as
`RESTIC_REPOSITORY`, `AWS_ACCESS_KEY_ID`, and so on.

The generated files therefore hold only tuning:

- the pgBackRest drop-in, with tuning from the `PGBR_*` settings, as before;
- the archive drop-in, `archive_command = 'pgbackrest archive-push %p'`;
- the timers.

No credential is in a file, a volume or `podman inspect`.

- **pgBackRest** runs inside `db`. `<stem>@.service` runs `podman exec … pgbackrest backup
  --type=%i`, started by the listed `-full`/`-incr`/`-diff` timers.
- **restic** is a one-shot quadlet named after the listed `restic-files-backup` timer. It runs
  in the ID range of the services that write the backed-up volumes, mounts them read-only, and
  uses a fixed `--host`.
- **Read mode: restoring another env's backups.** An env with `restore_from: <env>` in
  `info.yaml` (for the kinds the source lists under `backups`) and read credentials (`pgbr_s3_read_*` / `restic_read_*`, made by
  `dk <env> secrets read-key`) gets no backup units, no WAL archiving and no timers. The read keys
  become Podman secrets for the one-off containers of `db restore`, `db info`, `files restore` and
  `files snapshots`, listed under `restore` in the manifest; `db` never sees them. `db backup`,
  `db check`, `files backup` and `files init` are refused. Read and write credentials in one
  rendered env are refused for now: the restored cluster's system ID wouldn't match the env's own
  stanza (#204).

## Monitoring

Monitoring is per env and off by default, as on compose hosts (where nothing is monitored until
someone runs `dk <env> zabbix install`). An env opts in in its `info.yaml`:

```yaml
monitoring: zabbix   # omitted or false: no agent, no podman-ro-proxy, no status files
```

It needs `ops.zabbix` in tooling.yaml (the project's unit and userparams file names); the render
refuses `monitoring: zabbix` without it. Without `zbx_tlspsk` / `zbx_tlspskidentity` in the
env's credentials the render warns: the agent would talk to the server unencrypted.

With `monitoring: zabbix`, the render adds these parts:

- **The agent.** Zabbix Agent 2 is a root-owned system quadlet, so the account can't stop it.
  It runs as the image's unprivileged user, with no capabilities and active checks only.
- **`podman-ro-proxy`.** It passes on only read requests to the account's Podman API, for the
  Docker plugin.
- **`backup-status.sh` and a timer.** These write `pgbackrest info` and `restic snapshots` as
  JSON, which the agent reads through the same item keys as on Ubuntu hosts. The agent holds no
  credentials.
- **The status files' age.** A failed status run keeps the previous file, so `pgbackrest.info`
  and `restic.snapshots` would keep returning old data without any error. The rendered
  userparams alias each file's modification time (`vfs.file.time`) to a fixed key,
  `backup.status.pgbackrest.mtime` and `backup.status.restic.mtime`, for whichever backups
  the env has. Import [`zabbix/catalpa-backup-status-fcos.yaml`](zabbix/catalpa-backup-status-fcos.yaml)
  on the Zabbix server (Zabbix 7.0) and link it to the host. Its two triggers warn when a file
  hasn't been written for `{$BACKUP.STATUS.MAX_AGE}` (3h: three missed hourly runs), or is
  missing for that long. On a host with only one kind of backup, disable the other trigger.
  Tested against Zabbix 7.0.32 with the 7.0 agent: the import is idempotent and exports back
  unchanged, a 5-hour-old file fires its trigger, and a missing file fires through `nodata()`.

The settings use the keys `dk <env> zabbix install` reads (`zbx_*`, `ZBX_*`). `zbx_tlspsk` and
`zbx_tlspskidentity` become secrets of the system's Podman, listed under `system_secrets` in
the manifest.

## The host: host.bu

`host.bu` is built from the install map, so each generated file is placed exactly once.
`dk <env> host ignition -o host.ign` compiles it with Butane (in Docker, `--strict`) and
refuses a result over DigitalOcean's 64 KiB user-data limit. The manifest itself is installed
too, as `<config_dir>/fcos-manifest.json`.

Beyond the files, it sets up:

- **Users.** `core` is for admins, with passwordless sudo, so only `fcos.admin_keys` log in
  as `core`. The account (`deploy`) has no sudo, and takes `fcos.ssh_authorized_keys`: the keys
  dk and CI use, so a stolen deploy key can change the stack but not become root. A key may
  be in both lists. Each login's keys are a rendered file, `~/.ssh/authorized_keys.d/dk`, so
  `apply` changes them on a running host. `afterburn-sshkeys@core.service` is masked:
  on DigitalOcean it would otherwise copy the droplet's account keys (the `--ssh-keys` given
  when it was created) to `core` at every boot, getting around `admin_keys`. The account's uid and subordinate IDs are
  pinned, so the data directories keep their owners.
- **Data volume.** The DO block storage volume is formatted on first boot only, so a rebuilt
  host keeps its data. Each bind volume's directory is owned by its writers' host uid.
- **The account's systemd.** Linger, the edge's socket and Podman's API socket, the timers,
  unprivileged ports from 80, and the registry login kept in the account's home.
- **Zincati's reboot window**, the hostname and the time zone.
- **Hardening.** SSH by key only, no root login, `AllowUsers core <account>`. LLMNR and mDNS
  are off.

The `-incr` template runs daily at 02:30, so on Sundays it falls inside the default Zincati
window (02:00–03:00). `Persistent=true` reruns a backup that a reboot interrupts.

## dk's stack commands on a quadlet env

Compose doesn't run these stacks, but every container keeps its compose service's name and has
its own unit in the account's systemd. So with `runtime: quadlet`, dk's everyday stack
commands go to the host over SSH, as the account in `docker_host`. They read only `docker_host`
and the committed `generated/fcos/manifest.json`, so there's no SOPS decrypt and no compose.

| Command | On the host |
|---|---|
| `dk <env> logs [-f] [--tail N] [--since T] [svc…]` | `journalctl --user -u <svc>.service`: container output and the unit's start, stop and failure messages in one stream. `--tail` counts lines of that merged stream, not per service |
| `dk <env> ps` | the stack units' state (the edge's socket included), then `podman ps -a` |
| `dk <env> exec [-T] [-u U] [-e K=V] [-w DIR] svc cmd…` | `podman exec` |
| `dk <env> manage [--user U] …` | `podman exec <web> ./manage.py …` as `stack.manage_user` |
| `dk <env> restart\|stop\|start [svc…]` | `systemctl --user` on those units. With no service, the whole stack: `stop` stops the edge's socket first, so a request can't start it again |

A bare `dk <env>` brings the host up to date, as `up -d` does on a compose env:

1. The committed render must match a fresh one, as for `check`, so only committed files
   reach the host.
2. Every secret the render needs must exist on the host. A unit started without one fails, so
   dk stops here and points to `secrets push`.
3. Files that differ are installed and the units they affect restarted, as `apply` does
   (it shows the plan and asks, unless `-y`).
4. Every image an account quadlet runs is pulled. A unit whose image changed is restarted,
   unless step 3 already did. Backup one-shots pick a new image up on their next run, and
   `migrate`/`collectstatic` re-run with `django` (`PartOf=`).
5. Stack units that aren't running are started, as `up -d` starts stopped services. Their
   `PartOf=` one-shots get `reset-failed` first, so a `migrate` that failed earlier runs again.
6. The stack's units must all be active afterwards.

Step 4 is how a new image under the same tag goes out with `fcos.auto_update` off. `--tag`
is refused: a quadlet env's images follow `image_tag` in `info.yaml`, through a render and a
commit. `--dry-run` shows the plan and the images it would pull. The system quadlets
(Zabbix) aren't pulled.

`up`, `down`, `pull`, `build`, `run`, `rm`, `wipe` and the other compose verbs are refused:
they would start a second stack beside the quadlets. `dk <env> docker …` still works against
the account's Podman socket.

`dk fetch db --env <env>` and `dk fetch media --env <env>` work too, for `via: dk` databases:

- **`fetch db`** runs `podman exec -u postgres <db> pg_dump -Fc` over the container's local
  socket, so it needs no password. It dumps the same database as the compose path
  (`DJANGO_APP_DB`/`DJANGO_DB` in the container). The dump replaces the local file only once
  it is a whole archive.
- **`fetch media`** rsyncs the media volume's directory on the data volume (`fcos.bind_volumes`;
  `ops.restic.data_volume`, default `django_media`) as the account, with no Docker volume
  lookup.

Backups (`files` is the media backup; #43 renames it `media`):

| Command | On the host |
|---|---|
| `dk <env> db info \| check \| version` | `pgbackrest` in `db`, which takes the repository from its Podman secrets |
| `dk <env> db configure stanza-create \| verify` | `pgbackrest stanza-create` / `check` |
| `dk <env> db backup [full\|incr\|diff]` (default `incr`) | starts the timers' own `…-pgbackrest-backup@<type>` unit, waits, shows its log |
| `dk <env> db pgdump [args]` | `pg_dump -Fc` to stdout, as for `fetch db` |
| `dk <env> files backup` | starts the restic unit, waits, shows its log |
| `dk <env> files snapshots \| check \| stats \| init` | a one-off restic container built from that unit's quadlet: same image, ID range, secrets and mounts |
| `dk <env> db restore [--dry-run] [pgbackrest args]` | as on compose envs: stops the app and `db`, runs `pgbackrest restore --delta` in a one-off container like `db` (its image, ID range, settings, secrets and mounts), then starts the app again if it was running (`db` alone if only `db` was). `db` is healthy only once WAL replay ends, so the app waits for recovery, up to `db`'s start timeout (15 min). On a host provisioned with `--no-start` both stay stopped, so the media can be restored before the app serves anything; `host provision` starts them. Then the `ops.post_db_restore` hooks run, then `ops.post_metabase_db_restore` (its manage commands, then a restart of its `restart_services`: the restore brought back the whole cluster, Metabase's database too), as on compose; a stack left stopped prints the commands to run once it's up. **In read mode** (`restore_from`) the one-off has the read key instead, and replays the source's WAL itself (no connections, no archiving), promotes and stops, because `db`'s own pgBackRest can't reach the source's repository; `--type=…` restores get `--target-action=promote` |
| `dk <env> restore [--snapshot S] [-- pgBackRest args]` | both of the below in one go: stops the app and `db`, restores the database, then the media, starts them again as found, then runs the `ops.post_db_restore` and `ops.post_metabase_db_restore` hooks. One confirmation. Restores what the env backs up or restores (`backups:` / `restore_from`) |
| `dk <env> files restore [SNAPSHOT]` | `restic restore SNAPSHOT:/backup/<vol> --target /backup/<vol> --delete` in the backup unit's ID range, so each media directory ends up exactly as in the snapshot, with the right owner. `latest` means the latest from this host (`--host`); in read mode, the latest of the source's snapshots with that path (`--path`), since compose snapshots carry random hostnames. A restored volume the edge also mounts (the media, for Caddy in its own ID range) then gets `chmod -R o+rX`, as this host's app writes its own files: prod on compose writes `2750`/`640`, readable there only because its Caddy is root |
| `dk <env> db restore --file PATH` / `--dumps` | replaces the app database with a local `pg_dump -Fc` archive (`--dumps`: `paths.fetch_db_dump`): stops the app (not `db`), copies the archive into `db` over SSH and checks its SHA-256, drops and recreates the database and runs `pg_restore` with the compose scripts, starts the app, then runs the `ops.post_db_restore` hooks (not `post_metabase_db_restore`: only the app database is replaced). Asks for the env name. The Metabase dump isn't restored on quadlet envs yet |
| `dk <env> pull_media [--target DIR]` | the same rsync as `fetch media` (see #43 on deprecating `pull_media`) |

The restores ask you to type the env name first, unless you pass dk's `--yes`. Unlike compose's
`files restore`, the media is restored in place rather than through a staging volume: a restore
that fails part-way leaves a mix. Run it again to finish.

`dk transfer SRC DST` works when either end is a quadlet env. The steps are the usual ones, each
end done its own way: a quadlet destination stops everything but `db` (the edge's socket
first), takes the dump through the restore above, and takes the media by rsync under
`podman unshare`, chowned to the media's ID range (`--delete`). The media goes through the
local session directory, by rsync only. A compose end works as before.

Refused, since the render owns them: `db init`, `install-systemd`, `files push`, and
`db configure` without a mode.

## On the host

Every command below first checks the render, as `check` does, and refuses to touch the
host if the committed files differ from a fresh render. It then works only from the committed
`generated/fcos/`. The host's address comes from `docker_host`; `--host ADDRESS` overrides it,
for example for a new droplet before its DNS record moves. `--dry-run`
shows what would happen.

Two logins, as on the host itself. `core` (passwordless sudo) reads and installs the files and
manages the system units, with one `sudo sh` per command and the script on stdin. The account
(`deploy`) owns the Podman secrets, its own systemd and the registry login.

| Command | What it does |
|---|---|
| `host ignition -o FILE` | Compile `host.bu` to Ignition, the droplet's user data |
| `host provision [--no-start]` | After the droplet booted from it: wait for SSH, check the files match, push every secret, log in to the image registry (from `registry_user`/`registry_token` in sops, or asks for the token), start the stack |
| `host login` | Log the account in to the image registry again from sops, and show who it's logged in as: after rotating the bot's token |
| `secrets push [NAME…] [--restart]` | Stream secrets from `credentials.yaml` into Podman secrets |
| `diff [--names-only]` | Compare the host with the render: each installed file's content, mode and owner, the data directories, the timers, the secret names |
| `apply [--no-restart] [-y] [--force]` | Install what differs, then reload systemd and restart only the units affected |

**Secrets.** Each value goes from `sops -d` over SSH stdin into
`podman secret create --replace`. It is never written to disk on either side, never on a
command line and never printed. The stack's secrets belong to the account, and
`system_secrets` (Zabbix's PSK) to root. Running containers keep the old value until they
restart: `secrets push` lists the units that use a pushed secret, and `--restart` restarts them.
Secrets on the host that the render doesn't list are reported, never removed.

**Diff and apply.**

- Each file is written to a temporary file next to its destination, given its owner and mode,
  then renamed over the old one, so a unit never reads half a file. The account's directories
  are created as the account's (Ignition and `install -d` would make them root's).
- A file the previously applied manifest installed but the render no longer has is removed.
- A missing data directory is created with its range's owner. An existing one with the wrong
  owner is reported and left alone: that means the ID range moved, which needs a person.
- A missing timer link is created and the timer started.
- **SSH keys.** Every file in `core`'s and the account's `~/.ssh/authorized_keys.d/` other
  than the rendered `dk` is removed: Ignition's (from older renders), afterburn's, or a key
  added by hand. A unit in `masked_units` that isn't masked is masked. `apply` refuses to change
  `core`'s keys unless one of your public keys (`ssh-add -L`, `~/.ssh/*.pub`) stays in
  `admin_keys`, so it can't lock you out of root; `--force` overrides.
- A unit restarts only when something it reads changed: its quadlet, its settings file, a file
  it mounts (`caddy.json`, the pgBackRest config) or, for `secrets push --restart`, a secret.
  Changing `db`'s files restarts `db`, and Requires= restarts `django` with it. A one-shot that
  belongs to a long-running unit (`migrate`, `collectstatic`, through `PartOf=`) runs again
  when that unit restarts. A one-shot a timer starts (restic) is never restarted. A changed
  timer is restarted, which reschedules it.
- Podman keeps an existing network or named volume when its `.network`/`.volume` file changes.
  `apply` says so and leaves it: stop its users, remove it, start them.
- Anything host.bu sets outside the install map (users, sysctl, sshd, Zincati) needs a new host.

**Provisioning a new host.**

```bash
dk <env> host create --dry-run   # the plan: volume (new, or the one kept), droplet, DNS
dk <env> host create             # then provisions it; on a new volume, restores first
```

`host create` (tooling 2.0 M6), in order:

1. checks the committed render is current, and that its Ignition fits DigitalOcean's 64 KiB of
   user data;
2. refuses if the droplet exists: Ignition runs only on first boot (`host provision` finishes
   one that exists; `host destroy --keep-volumes` makes room for a new one);
3. the data volume: the env's detached one is reused with its data (a rebuild after
   `host destroy --keep-volumes`), one attached to a droplet is refused, and otherwise a new one
   is created in the project;
4. creates the droplet from `digitalocean.image` with the volume attached (so Ignition formats
   or mounts it) and the Ignition as its user data, without DigitalOcean's metrics agent;
   then puts it behind a cloud firewall, `dk-<droplet>`: SSH, HTTP, HTTPS, HTTP/3 and ping in;
   out, only DNS, HTTP(S), NTP, mail, git over SSH and Zabbix (active checks, all a rendered
   host's agent makes). `digitalocean.firewall: false` for none, `{out_tcp: [5432]}` for more;
5. points the env's DNS A records at it, and `docker_host` too when it names an IP (a hostname
   stays);
6. runs `host provision` against the new IP; `--no-provision` stops before it.
7. **on a new data volume, restores before starting**: from `restore_from`'s backups, or the
   env's own (`backups:`), whatever the repositories hold (a repository with no backup yet is
   skipped, and that part starts empty). It provisions without starting, runs `dk <env> restore`
   by IP (no confirmation: the volume is empty), then starts the stack and runs the
   `ops.post_db_restore` and `ops.post_metabase_db_restore` hooks. A rebuilt prod thereby continues its backup chain; starting
   empty would make a cluster its own repository refuses. A reused volume keeps its data and
   gets no restore; `--empty` starts with no data on purpose; `--no-start` restores and stops
   there.

`host ignition -o FILE` still compiles the Ignition on its own, for another provider.

Provisioning is safe to run again. The registry login is kept in the account's home
(`~/.config/containers/auth.json`, where host.bu points systemd's `REGISTRY_AUTH_FILE`).

**Registry login.** The host pulls private images with nobody connected, so the account keeps
its own login. Keep the bot's in the project's `credentials.yaml` (`dk secrets`, which edits
`docker/credentials.yaml` with sops):

```yaml
registry_user: <the bot's GitHub login>
registry_token: <a classic PAT with only read:packages, with an expiry date>
```

`host provision` then logs in without asking, the token going over SSH's stdin to
`podman login --password-stdin`: never on a command line, never printed. Without the two keys
it asks, as before. A provisioned host keeps its login, so after rotating the token run
`sops`/`dk secrets`, then `dk <env> host login`.

**The project's `credentials.yaml`** holds what every env shares. It needs its own creation rule
in `.sops.yaml` (`path_regex: ^docker/credentials\.yaml$`), which can name fewer people than the
env files' rule. An env's own `credentials.yaml` overrides a project value of the same name, and
the render sees both files' names, so a secret can live in either. Compose envs don't read it
yet.

**Restoring.** Run `host provision --no-start`: on an empty data volume, starting `db` would
initialise an empty cluster. Restore the database and media, then run `host provision`
again. Creating the pgBackRest stanza and the restic repository on a new, empty repository is
still manual (catalpa-site `docker/envs/fcos-rootless/README.md`).

**Josh's `host create` for Fedora CoreOS** (`feat/host-create-fcos-firewall`) builds its own
Ignition, in which `core` runs the stack and has no sudo. For an env with `runtime: quadlet`,
it should take `fcos.host.compile_ignition(generated_dir)` instead, then run `provision`. Its
firewall and DNS steps apply unchanged.
