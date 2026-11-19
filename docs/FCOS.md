# Fedora CoreOS: rootless quadlets rendered from compose

`dk <env> fcos render` turns an environment's compose stack into the files a Fedora CoreOS
host needs to run it as rootless Podman quadlets. The files go in
`docker/envs/<env>/generated/fcos/`, and you commit them. `dk <env> fcos check` compares the
committed files with a fresh render, so what reaches a host is what was reviewed.

This is the first part of #182 ("option B"). It doesn't install anything on a host yet. Still
to come: the host's Butane config, backups and monitoring, then pushing secrets and applying
the files. The design and the proof of concept are in catalpa-site's
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
| `env/<service>.env` | `/etc/<project>/` | settings without credentials |
| `systemd/<proxy>.socket` | `~<user>/.config/systemd/user/` | opens 80/443 for the edge |
| `caddy/caddy.json` | `~<user>/.config/<project>/` | the edge's config |
| `manifest.json` | — | ID ranges and host uids, bind volumes, secrets, images, the install map |

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
