# Fedora CoreOS: rootless quadlets rendered from compose

`dk <env> fcos render` turns an environment's compose stack into the files a Fedora CoreOS
host needs to run it as rootless Podman quadlets. The files go in
`docker/envs/<env>/generated/fcos/`, and you commit them. `dk <env> fcos check` compares the
committed files with a fresh render, so what reaches a host is what was reviewed.

This is #182 ("option B"). A render holds everything a new host needs, including its Butane
config (`host.bu`), backups and monitoring. Pushing the secrets and applying the files to a host
come later. The design and the proof of concept are in catalpa-site's
`docker/envs/fcos-rootless/` (catalpa-site#1006).

## Principles

- **The compose file stays the description of the stack.** Plain `docker compose up` must keep
  working in the repository without `dk`, which matters when a project is handed over. Nothing
  here needs a compose or Caddyfile change that only works under `dk`.
- **Plain compose refuses to start without a secret.** Write each credential as
  `${KEY:?KEY must be set}`, never with a default. `fcos check` enforces this: for each
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
fcos:                          # every key is optional
  user: deploy                 # the unprivileged account that runs the stack
  uid: 1001
  data_mount: /var/mnt/data    # where the block storage volume is mounted
  bind_volumes: [pgdata, django_media, caddy_data]
  id_ranges: {db: 10000}       # pin a group's ID range, by any service in the group
  auto_update: false           # podman auto-update (staging/test sites; prod deploys explicitly)
  drop_ports: [mailpit]        # services whose published ports are dropped, not refused
  caddyfile: /etc/caddy/Caddyfile   # the Caddyfile's path inside the edge image
  volume_owners: {pgdata: 999} # uid inside its range that owns a bind volume (default 0;
                               # the pgBackRest data volume 999, postgres)
  # The host (host.bu):
  ssh_authorized_keys:         # required: public keys for core (admins) and the account (dk)
    - ssh-ed25519 AAAA… someone
  hostname: catalpa-site-prod  # default <project>-<env>
  timezone: Asia/Dili          # default tooling.yaml digitalocean.timezone, else UTC
  block_volume: catalpa-site-prod-data  # DO volume to format and mount; default <hostname>-data;
                               # false: data stays on the root disk
  update_window: {days: [Sun], start: "02:00", minutes: 60}   # when Zincati may reboot
  zabbix_image: docker.io/zabbix/zabbix-agent2:alpine-7.0-latest
```

With `runtime: quadlet`, `dk <env>` accepts only `info`, `secrets`, `host`, `docker` and
`fcos`. The compose commands would find no stack on that host, or would start a second one.

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

These use the same keys as compose deploys:

- `pgbr_s3_write_*` and `restic_write_*` in the env's `credentials.yaml`;
- `ops.pgbackrest` and `ops.restic` for what to back up;
- `ops.systemd_units` for which timers exist and which are enabled, by the names listed there.

Schedules come from `dk`'s own timer templates, so both runtimes back up at the same times, in
the host's time zone.

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
- **Only write mode is rendered.** An FCOS host backs itself up, so the `*_read_*` keys are
  left out.

## Monitoring

When tooling.yaml has `ops.zabbix`, the render adds three parts:

- **The agent.** Zabbix Agent 2 is a root-owned system quadlet, so the account can't stop it.
  It runs as the image's unprivileged user, with no capabilities and active checks only.
- **`podman-ro-proxy`.** It passes on only read requests to the account's Podman API, for the
  Docker plugin.
- **`backup-status.sh` and a timer.** These write `pgbackrest info` and `restic snapshots` as
  JSON, which the agent reads through the same item keys as on Ubuntu hosts. The agent holds no
  credentials.

The settings use the keys `dk <env> zabbix install` reads (`zbx_*`, `ZBX_*`). `zbx_tlspsk` and
`zbx_tlspskidentity` become secrets of the system's Podman, listed under `system_secrets` in
the manifest.

## The host: host.bu

`host.bu` is built from the install map, so each generated file is placed exactly once.
Compile it in `generated/fcos/`:

```bash
podman run --rm -v "$PWD":/work:Z -w /work quay.io/coreos/butane:release \
  --strict --files-dir . host.bu > host.ign
```

Beyond the files, it sets up:

- **Users.** `core` is for admins, with passwordless sudo. The account (`deploy`) has no sudo,
  and its uid and subordinate IDs are pinned, so the data directories keep their owners.
- **Data volume.** The DO block storage volume is formatted on first boot only, so a rebuilt
  host keeps its data. Each bind volume's directory is owned by its writers' host uid.
- **The account's systemd.** Linger, the edge's socket and Podman's API socket, the timers,
  unprivileged ports from 80, and the registry login kept in the account's home.
- **Zincati's reboot window**, the hostname and the time zone.
- **Hardening.** SSH by key only, no root login, `AllowUsers core <account>`. LLMNR and mDNS
  are off.

The `-incr` template runs daily at 02:30, so on Sundays it falls inside the default Zincati
window (02:00–03:00). `Persistent=true` reruns a backup that a reboot interrupts.
