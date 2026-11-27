# Evidence: several envs on one host (2026-11-27)

The VM: Fedora CoreOS 45.20260913.1.1 (QEMU on a laptop, user-mode network, 2.5 GB, 2 vCPU),
Podman 6.1.1, systemd 261. Throwaway: an overlay disk, a key made for it, no production data,
a demo project with an empty database. `vm <user> <cmd>` is ssh to that account; `dk` runs this
branch's dk from `demo/`. Zincati was masked for the run (it found an update and would have
rebooted). Output is trimmed to the lines that matter; nothing else was edited.

## Part 1: unit versions, raw Podman

One template, `demo-web@.container`, in a "units version 9" and a "units version 10" copy that
differ in `WHOAMI_NAME` (and, for the drop-in, the image tag).

### (b) in one account: two applications shipping the same file name

```console
$ podman quadlet install --application demo36 --reload-systemd=false a36/   # v9
/var/home/env-a/.config/containers/systemd/demo36/demo-web@.container
$ podman quadlet install --application demo38 --reload-systemd=false a38/   # v10
/var/home/env-a/.config/containers/systemd/demo38/demo-web@.container
exit=0
$ podman quadlet list
NAME                 UNIT NAME          PATH ON DISK                                                           STATUS      APPLICATION
demo-web@.container  demo-web@.service  /var/home/env-a/.config/containers/systemd/demo38/demo-web@.container  Not loaded  demo38
$ systemctl --user daemon-reload; systemctl --user cat demo-web@x.service
Description=demo web %i (units version 9)
SourcePath=/var/home/env-a/.config/containers/systemd/demo36/demo-web@.container
```

Both installs succeed, the list shows demo38's file, and the generator runs demo36's. No
warning anywhere.

### (a) version-named templates in one application

```console
$ systemctl --user start demo-web-v9@s36 demo-web-v10@s38
demo-web-v9-s36 Up Less than a second
demo-web-v10-s38 Up Less than a second
demo-web-v9-s36: WHOAMI_NAME=s36-units-v9
demo-web-v10-s38: WHOAMI_NAME=s38-units-v10
```

### (c) one template and an instance drop-in

A drop-in folder alone (`demo-web@s38.container.d/10-v10.conf`) changed nothing:

```console
ExecStart=/usr/bin/podman run --name demo-web-%i … --env WHOAMI_NAME=%i-units-v9 docker.io/traefik/whoami:v1.10
```

With an instance file beside the template (`demo-web@s38.container`, a copy of it):

```console
-- s38:
Description=demo web %i (units version 10, by drop-in)
ExecStart=/usr/bin/podman run --name demo-web-%i … --env WHOAMI_NAME=%i-units-v10 docker.io/traefik/whoami:v1.11
-- s36:
ExecStart=/usr/bin/podman run --name demo-web-%i … --env WHOAMI_NAME=%i-units-v9 docker.io/traefik/whoami:v1.10
```

Partisipa's installer relies on the same rule (`infra/quadlets/bin/install-units` on
`develop`: "Quadlet varies one instance of a template only through an instance file beside it
… and that instance's drop-ins").

### (d) one account per env, the same file names

```console
== env-a on v9, env-b on v10, same unit name demo-web@staging
env-a: WHOAMI_NAME=staging-units-v9
env-b: WHOAMI_NAME=staging-units-v10
== upgrade env-a alone to v10
env-a: WHOAMI_NAME=staging-units-v10
env-b: demo-web-staging Up About a minute
== roll env-a back to v9
env-a: WHOAMI_NAME=staging-units-v9 07:34:32
env-b: WHOAMI_NAME=staging-units-v10 07:32:10
```

Each step is `podman quadlet install --replace --application demo <dir>`, daemon-reload,
restart, in that account only. env-b's container kept its 07:32:10 start throughout.

## Part 2: dk, three envs on one host

`demo/` has one compose file (Postgres, whoami, a Caddy front built into a throwaway local
registry) and three envs. Each env's `info.yaml` differs only in its name, account, uid,
`subuid_start` and `local_port`:

```yaml
fcos:
  user: env-b
  uid: 1002
  subuid_start: 655360    # this account's subordinate IDs on the shared host
  local_port: 8102        # behind the host edge: 127.0.0.1:8102, plain HTTP
```

### Render

```console
$ dk a render
docker/envs/a/generated/fcos: 14 files, 14 changed. …
$ cat docker/envs/a/generated/fcos/systemd/caddy.socket
[Socket]
ListenStream=127.0.0.1:8101
```

The rendered `caddy.json`: one server, `"listen": ["fd/3"]`, `automatic_https.disable`,
`trusted_proxies` 127.0.0.1/32 and ::1/128, the project's route to `web:80` unchanged.

Install destinations across the three renders, checked for clashes:

```console
37 destinations; clashes: {"/var/home/core/.ssh/authorized_keys.d/dk": ["a", "b", "c"]}
```

(before the settings-folder fix, `/etc/demo/*.env` and the deploy record were shared too).

### The host's Ignition, from the envs' host.bu

```console
$ python merge_host_bu.py merged.bu host-edge demo/docker/envs/a demo/docker/envs/b demo/docker/envs/c
conflicts between envs:
  file /etc/systemd/user.conf.d/10-registry-auth.conf: envs write it differently
  file /etc/ssh/sshd_config.d/10-hardening.conf: envs write it differently
```

```diff
<           DefaultEnvironment=REGISTRY_AUTH_FILE=/var/home/env-a/.config/containers/auth.json
>           DefaultEnvironment=REGISTRY_AUTH_FILE=/var/home/env-b/.config/containers/auth.json
<           AllowUsers core env-a
>           AllowUsers core env-b
```

After the merge resolves those (union of `AllowUsers`; registry auth per account):

```console
merged.bu: 5 accounts, 113 storage entries
$ butane --strict --files-dir . < merged.bu > merged.ign    # exit 0
30484 bytes
```

On the booted host:

```console
$ cat /etc/subuid
core:524288:65536
env-a:589824:65536
env-b:655360:65536
env-c:720896:65536
edge:786432:65536
$ ls -la /etc/containers/systemd/users/
drwxr-xr-x. 2 root root … 1001
drwxr-xr-x. 2 root root … 1002
drwxr-xr-x. 2 root root … 1003
drwxr-xr-x. 2 root root … 1010
$ sudo ls -ln /var/mnt/data/env-b
drwx------ 2 665359 665359 … caddy_data
drwx------ 2 671358 671358 … pgdata
env-a: podman.socket active, caddy.socket 127.0.0.1:8101
env-b: podman.socket active, caddy.socket 127.0.0.1:8102
env-c: podman.socket active, caddy.socket 127.0.0.1:8103
```

### Deploy

```console
$ dk a secrets push
  secret postgres_password: pushed
$ dk a
spike-vm: files match the render.
spike-vm: pulling 3 images …
  caddy.service                active
  db.service                   active
  web.service                  active
dk a: up to date.
```

envs b and c the same, except that their apply stopped to ask about core's shared key file:

```console
~ /var/home/core/.ssh/authorized_keys.d/dk (comments only)
-# Generated by `dk a render` …
+# Generated by `dk b render` …
Apply this to spike-vm (b)? [y/N]
```

(answered with `--yes`). Through the host edge, from the laptop:

```console
a: Name: env-a
b: Name: env-b
c: Name: env-c
== a.spike.test
X-Forwarded-For: 10.0.2.2, 127.0.0.1
HTTP 200
== http redirect
308 -> https://a.spike.test/
```

10.0.2.2 is the laptop as the VM sees it: the visitor's address survives both hops.

### Restore one env

Each env's database got a marker row; env b was dumped, changed, then restored with dk.

```console
b before restore: b original,b changed after the dump
$ dk b db restore --file b-seed.custom --yes
b: systemctl --user stop caddy.socket caddy.service web.service
db restore: pg_restore …
b: systemctl --user start caddy.service web.service caddy.socket
db restore: done.
a: a original
b: b original
c: c original
start-time changes:
  > env-b caddy=1795765862
  > env-b web=1795765862
```

(The first try failed on the demo's settings: restore expects `DJANGO_DB`/`DJANGO_DB_USER`
on the db service and otherwise uses `catalpa_db`. It stopped only env b's app.)

### Upgrade one env, then roll it back

```console
$ sed -i 's/env_label: env-a}/env_label: env-a-v2}/' docker/envs/a/info.yaml; dk a render; git commit
~ envfiles/web.env
$ dk a --yes
Restarts: web.service
a: Name: env-a-v2
b: Name: env-b
c: Name: env-c
start-time changes:
  > env-a web=1795765898

$ git revert --no-edit HEAD
$ dk a check
docker/envs/a/generated/fcos: matches a fresh render; every credential is required.
$ dk a --yes
Restarts: web.service
a: Name: env-a
start-time changes:
  > env-a web=1795765927
```

### Remove one env (by hand: there is no dk verb yet)

```console
1. stop env c's units and remove its secrets (as env-c)        → 0 secrets left
2. remove its units, settings and user socket (root, via core)  → 0 files left
3. its data folder: kept here; a real remove asks               → 50M
4. take its route off the host edge                             → edge active
a: 200 Name: env-a
b: 200 Name: env-b
c: 000                     (no certificate or route for it any more)
start-time changes for a and b: (none)
```

The edge was restarted to read its new config. A restart drops open connections for every env
(inferred, not measured); a real remove should reload Caddy instead. `host-edge/` in this
branch is the edge after this step, with env c's route gone.

### Memory

```console
env-a all processes: 59 MB
env-b all processes: 51 MB
env-c all processes: 27 MB      (everything stopped)
edge all processes: 78 MB
```

## Part 3: checks from the literature review (2026-11-28)

**Limiting loopback ports to the site's own accounts.** One nftables rule; pasta connects as
the account's uid, so `meta skuid` matches:

```console
table inet dk_loopback { chain output { type filter hook output priority 0; policy accept;
  oif "lo" ip daddr 127.0.0.1 tcp dport { 5433, 8333 } meta skuid != { 1021, 1022, 1023 } counter reject with tcp reset } }
== before the rule
env-b: 169.254.1.3:5433 - accepting connections
blue: 169.254.1.3:5433 - accepting connections
== after the rule
env-b: 169.254.1.3:5433 - no response
blue: 169.254.1.3:5433 - accepting connections
host core: /dev/tcp/127.0.0.1/5433: Connection refused
counter packets 2
```

**Per-account Quadlet folders `root:<gid> 0750`:**

```console
== before: blue lists other accounts' files
7
drwxr-x---. 2 0 1002 126 … 1002            (and 1001, 1003, 1010 the same)
== after: blue lists
0
env-b still loads its own: caddy.service, db.service, web.service, web-task.service generated
env-b: touch /etc/containers/systemd/users/1002/x: Permission denied
```

**Removing an env in order.** The first run's output was lost when this laptop's `/tmp` quota
ran out (the VM disk was in RAM-backed `/tmp`). The VM's disk was copied mid-run with env-c's
data and home moved aside and env-c out of `AllowUsers`, so the run was finished after a
reboot:

```console
linger: blue edge env-a env-b green site       (env-c's gone)
processes: 0
/etc/subuid:env-c:720896:65536
$ sudo userdel env-c                            # exit 0
id: 'env-c': no such user
subuid/subgid: no env-c lines left              # userdel removes the ranges itself
$ ls /var/lib/dk/retired
env-c-20261127-data  env-c-20261127-home
```

## Part 4: one folder shared by blue and green, without S3 (2026-11-28)

The containers run as uid 1000, as Partisipa's backend does (`USER appuser`, uid 1000).
Partisipa mounts its media today with `:z,U`. `U` changes the folder's owner to the
container's user, which can't work for a folder two accounts share.

### A: an access list naming each colour's mapped user

blue's uid 1000 is host uid 917504+999 = 918503; green's is 983040+999 = 984039.

```console
$ setfacl -m u:918503:rwx,u:984039:rwx,d:u:918503:rwx,d:u:984039:rwx,d:m::rwx /var/srv/media-acl
== blue writes a.txt and sub/
== green reads, appends, writes sub/b.txt       from-blue … (green sees blue's files as 65534)
== blue reads green's file, deletes it          from-blue green-was-here from-green deleted
== blue: chmod 644 (Django's default FILE_UPLOAD_PERMISSIONS); green appends
sh: can't create /media/c.txt: Permission denied   (green can still read and delete it)
== blue: chmod 664; green appends               append-ok
```

### B: a shared group kept inside the container

`site-media` is gid 3000, with blue and green as members. The folder is `root:site-media 2770`
with a default ACL `g::rwx`, and the containers run with
`--userns=keep-id:uid=1000,gid=1000 --group-add keep-groups`:

```console
== blue / green / blue: write, read, append, write in sub/, delete each other's   (all ok)
$ ls -ln /var/srv/media-group      (on the host: files owned by the real accounts)
-rw-rw----+ 1 1022 3000 25 a.txt
-rw-rw-r--+ 1 1023 3000  4 d.txt
== chmod 644 → the other colour can't append; chmod 664 → append-ok
== as a systemd user service, the user manager having started before the group was added
uid=1000(core) gid=1000(1000) groups=1000(1000)
ls: can't open '/media': Permission denied
$ sudo systemctl restart user@1022.service      → the same service lists /media
```

### Both, from a third account (env-b)

```console
ls: can't open '/m1': Permission denied        (group folder)
ls: can't open '/m2': Permission denied        (access-list folder)
```

### SELinux label

Fedora CoreOS has no `semanage`, and `/var/srv` defaults to `var_t`, so a relabel would undo a
`chcon`. A line in `file_contexts.local` makes the container label the default:

```console
$ echo '/var/srv/media-(acl|group)(/.*)?  system_u:object_r:container_file_t:s0' >> /etc/selinux/targeted/contexts/files/file_contexts.local
$ chcon -R -t var_t /var/srv/media-acl; restorecon -Rv /var/srv/media-acl
Relabeled /var/srv/media-acl from …:var_t:s0 to …:container_file_t:s0
blue: ls /media → a.txt c.txt e.txt sub
```

### C: `:idmap` with another account's user (ruled out)

```console
green$ podman run … -v '/var/srv/media-idmap:/media:idmap=uids=@918503-1000-1;gids=@918503-1000-1' …
Error: could not find a user namespace mapping for the relative mapping "@918503-1000-1"
```

A rootless account can only map IDs inside its own ranges.

## Not tested

Backups (pgBackRest and restic per env), Zabbix on a shared host, `host create` on
DigitalOcean, the units-image app envs themselves, and whether an app front's published port
(rather than a socket) keeps the visitor's address.

## Mistakes on the way, kept out of the result

- A hand-written Ignition gave core a literal `{k}` for its key; rebuilt.
- Hand-made links created each account's `~/.config` as root, which breaks rootless Podman. dk's
  own `host.bu` owns every parent directory; the second VM used it.
- `network-online.target` was never started on the hand-made host, so a restart waited
  forever. dk's `host.bu` already pulls it in.
- The first host edge relabelled a root-owned `/etc` file (`:Z`) and failed; its config moved
  to the edge account's home, as dk does for an env's Caddy.
- The same five tests fail on untouched `dev-2.0` on this laptop (Butane and Fedora in
  containers can't read pytest's temporary files); the spike changes no test outcome.
