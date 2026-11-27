# A server shared by several envs: how to

Why it works this way: [MULTI_ENV_HOST.md](MULTI_ENV_HOST.md). A complete example, used for
the VM proof, is in [examples/shared-host/](examples/shared-host/): the server `shared-1`, a
plain env (`nextgen`) and a blue/green site (`partisipa`).

## 1. Describe the server

A folder with a `host.yaml`. Put it in the project (`docker/hosts/<host>/`) when one project
uses the server, or in a hosts repo checked out beside the projects when several do.

```yaml
hostname: shared-1
timezone: Asia/Dili
admin_keys: [ssh-ed25519 AAAA… admin]          # core: admins, root through sudo
ssh_authorized_keys: [ssh-ed25519 AAAA… dk]    # every account: dk and CI, no sudo
update_window: {days: [Sun], start: "02:00", minutes: 60}
edge: {tls: acme, email: ops@example.org}       # tls: internal for a test server
retired_slots: []                               # slots of accounts gone with an old server
accounts:
  - {name: deploy, slot: 1, project: nextgen, env: staging, names: [nextgen.example.org]}
  - {name: site, slot: 2, project: partisipa, env: staging, role: site,
     names: [partisipa.example.org, metabase.partisipa.example.org]}
  - {name: blue, slot: 3, project: partisipa, env: staging, role: blue}
  - {name: green, slot: 4, project: partisipa, env: staging, role: green}
```

- An env has one account (role `env`, the default) or, for a blue/green site, exactly `site`,
  `blue` and `green`. `project` is the project's tooling.yaml name; `env` its env's name.
- `names` are what the edge serves: a plain env's account, or a site's `site` account.
- A key line may start `cert-authority` (an SSH CA for that login).
- `insecure_registries: [host:port]` only for a test registry (the VM's).

Then:

```console
$ dk host docker/hosts/shared-1 render     # writes docker/hosts/shared-1/generated/: commit it
$ dk host docker/hosts/shared-1 check      # fails if generated/ differs from a fresh render
$ dk host docker/hosts/shared-1 ignition -o shared-1.ign
```

Boot a new Fedora CoreOS server with that Ignition (DigitalOcean user data, or QEMU's
`-fw_cfg name=opt/com.coreos/config,file=shared-1.ign`). It comes up with every account, its
pinned ID range, linger, a `0750` quadlet folder, the edge on 80/443 and the loopback
firewall. **Every account comes from this first boot.** To add or remove an env, change
host.yaml, render, and build a new server; dk refuses to put an env on a server that lacks its
account.

Each account's numbers come from its slot: uid `1000 + slot`, subordinate IDs from
`589824 + (slot − 1) × 65536`, loopback ports `20000 + 10 × slot` to `+ 9`. The edge is slot 31.

## 2. A plain env on it

info.yaml gives `host:` (the folder, relative to the project) instead of the server settings
host.yaml owns (`fcos.user`, `uid`, `hostname`, `timezone`, `admin_keys`,
`ssh_authorized_keys`, `update_window`, `storage.data_volume`):

```yaml
runtime: quadlet
host: ../catalpa-hosts/shared-1
docker_host: ssh://deploy@shared-1.example.org/run/user/1001/podman/podman.sock
site_origin: [https://nextgen.example.org]
image_registry: ghcr.io/catalpainternational
image_tag: v1.2.3
```

Everything else is as for an env with a server of its own (docs/FCOS.md): `dk <env> render`,
commit, `dk <env> secrets push`, then a bare `dk <env>`. The env's proxy listens on its
account's first loopback port in plain HTTP; the edge does TLS. Its data is in
`/var/mnt/data/<account>/`, its settings in `/etc/<project>/<env>/`. `dk <env> host
ignition|provision` refuse: the server is `dk host`'s.

## 3. A blue/green site on it

The project ships its units in a folder (Partisipa's `infra/quadlets`), with an `app.toml`:

```toml
set = "notes"              # the unit set: secrets are <set>-<instance>-<name>
health = "/healthz"        # asked on a colour's port before the switch

[site]                     # the site account: shared by both colours
units = ["notes-site@.pod", "notes-db@.container", "notes-front@.container", "notes-site@.target"]
start = ["notes-site@{env}.target"]
secrets = ["postgres-password"]

[colour]                   # the blue and green accounts, each at its own version
units = ["notes@.pod", "notes-migrate@.container", "notes-web@.container", "notes@.target"]
start = ["notes@{env}-{colour}.target"]
secrets = ["dotenv"]

[ports]                    # "<site|colour>:<unit>:<container port>"
edge = "site:notes-site@.pod:80"       # the edge sends the site's names here
colour = "colour:notes@.pod:8000"      # the site's front sends visitors here
db = "site:notes-site@.pod:5432"       # colours reach it at 169.254.1.3:<port>

[hooks]                    # run in the site account from the site version's bin/
frozen = "bin/snapshot-db {env} frozen"     # 0: a database is set aside; 1: none; else abort
freeze = "bin/snapshot-db {env} freeze"
thaw = "bin/snapshot-db {env} thaw"
done = "bin/snapshot-db {env} take-frozen"  # prints the snapshot's path
switch = "bin/switch-site {env} {colour} {port}"
rollback = "bin/snapshot-db {env} restore {snapshot}"
keep = "bin/snapshot-db {env} keep"         # optional: dump what a rollback throws away, first
```

info.yaml says which version each account runs, a git ref of the project or a folder inside
`units` (or both):

```yaml
runtime: quadlet
host: ../catalpa-hosts/shared-1
docker_host: ssh://site@shared-1.example.org/run/user/1002/podman/podman.sock
bluegreen:
  units: infra/quadlets
  site: {ref: v3.8}
  blue: {ref: v3.7}
  green: {ref: v3.8}
```

credentials.yaml (sops) gives each account's secrets:

```yaml
site:
  postgres-password: …
blue:
  dotenv: |
    DATABASE_URL=postgresql://app:…@169.254.1.3:20021/app
green:
  dotenv: |
    DATABASE_URL=postgresql://app:…@169.254.1.3:20021/app
```

Then:

```console
$ dk staging render              # generated/fcos/{site,blue,green}/: commit
$ dk staging secrets push        # each account's secrets
$ dk staging deploy              # the colour that isn't live (the first deploy: blue)
$ dk staging colours             # which is live, what each runs, the rollback record
$ dk staging rollback            # back one deploy: the colour and the database (writes since are
                                 # lost from the site; a keep hook, if app.toml has one, dumps them first)
```

To release: change the idle colour's `ref` in info.yaml, `dk staging render`, commit, then
`dk staging deploy`.

`deploy` follows Partisipa's site deploy:
1. Take a lock. Refuse if an earlier deploy left a mark.
2. Install the site's files, and the idle colour's files and images, while the site still
   serves. Clear the old rollback record: the idle colour no longer holds what it named.
3. Ask `frozen`. Write the rollback record, then the mark.
4. Take the live colour out of the boot. Stop it and everything its target wants, waiting
   for all of it.
5. `freeze`.
6. Start the idle colour. Wait for every unit its target wants: a failed unit fails at once.
7. Check health on its port, then `switch`.
8. Enable the new live colour at boot. Run `done`, record the snapshot, and clear the mark.

A failure after the stop puts things back: it stops the new colour, runs `thaw`, starts the
old colour, switches back and puts it back in the boot. If the put-back fails, the mark stays,
no colour starts at boot, and every deploy is refused until `rollback`. `rollback` recovers
from that too: with no snapshot to restore, it thaws whatever is still set aside, then starts
the old colour.

The server's site units restart when their files change, before the outage begins; the
"down N s" a deploy prints doesn't count that.

dk installs each account's units as root (quadlet files in its quadlet folder, other systemd
units in `~/.config/systemd/user/`), plus one drop-in per unit that publishes a port: the
`Network=` reset, `Network=pasta:--map-host-loopback,169.254.1.3`, and
`PublishPort=127.0.0.1:<slot port>:<container port>`. Pods should set `ExitPolicy=continue`,
or a migrate oneshot exiting stops the pod.
