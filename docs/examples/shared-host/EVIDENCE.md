# Evidence: Nextgen and Partisipa on one server (VM, 28 Nov 2026)

These are stand-ins, with made-up data and public or test-registry images:

- **`nextgen/`**: a plain env, a compose stack (Postgres, whoami, a Caddy front), in account
  `deploy`;
- **`partisipa/`**: a blue/green site running the Partisipa-shaped test app
  (`tests/fixtures/fcos/bluegreen-app`), in accounts `site`, `blue` and `green`;
- **`shared-1/`**: the server both name in their `host:`.

**Where it ran:** a local QEMU VM, Fedora CoreOS 45.20260913.1.1, Podman 6.1.1, 2.5 GB. New
servers boot stable 45.20261030.3.1 with Podman 6.1.3 (containerwharf#442); the final Ignition
hasn't been booted on stable. It
was booted from `dk host shared-1 ignition` and reached as `shared-vm`. Images came from a
registry on the laptop. A probe asked both sites through the shared edge every 0.2 s.

Output is trimmed to the lines that matter. Everything is "(measured)".

## The server, from its first boot

```console
$ dk host shared-1 render && dk host shared-1 ignition -o shared-1.ign
shared-1.ign: 15885 bytes (DigitalOcean user data: 64 KiB at most)
```

```console
$ cat /etc/subuid
core:524288:65536
deploy:589824:65536
site:655360:65536
blue:720896:65536
green:786432:65536
edge:2555904:65536
$ ls -ln /etc/containers/systemd/users/
drwxr-x---. 2 0 1001 … 1001          (1002, 1003, 1004 and 1031 the same)
$ sudo nft list table inet dk_loopback | grep -c reject
4
AllowUsers core deploy site blue green
```

## Both projects arrive with dk

```console
$ (nextgen) dk staging secrets push && dk staging
dk staging: up to date.          caddy.service active, db.service active, web.service active
$ (partisipa) dk staging secrets push
  secret notes-staging-postgres-password: pushed        (site)
  secret notes-staging-blue-dotenv: pushed              (blue)
  secret notes-staging-green-dotenv: pushed             (green)
$ (partisipa) dk staging deploy
blue is live (the first deploy: nothing was stopped).
nextgen: 200   partisipa: 200
```

## The same unit names at different versions, in two accounts

```console
1003/notes-web@.container      3a784ad205de UNITS_VERSION=1      (blue)
1004/notes-web@.container      d5d180ebba2e UNITS_VERSION=2      (green)
1003/notes-migrate@.container  2b4b3e5300d6 UNITS_VERSION=1
1004/notes-migrate@.container  eb27d89bb19c UNITS_VERSION=2
```

## A blue/green switch while Nextgen keeps serving

```console
$ dk staging deploy                       # blue (v1) is live; green gets v2
Stopping blue: the site is down until green answers.
  site: bin/snapshot-db staging freeze
  site: bin/switch-site staging green 20040
green is live; the site was down 4 s.
{"colour": "green", "units": "2", "notes": ["first-note"], …}
probe: 24× p=200 blue n=200 · 1× p=503 n=200 · 12× p=200 blue n=200 · 5× p=200 green n=200
nextgen: unchanged (its containers' start times and a hash of every file dk installed)
```

That run found a bug, since fixed. The 12 blue answers after the 503 came from a blue that
was still stopping: `systemctl --user stop <target>` returns before the units it wants have
stopped, and the test app's web takes 10 s to die. dk now stops the target and every unit it
wants in one call, which waits for them.

The same flaw is in containerwharf's site deploy (Vikunja #2389). A later deploy after the
fix:

```console
$ dk staging deploy
blue is live; the site was down 18 s.          (10 s of it: the test app ignoring SIGTERM)
probe: 52× p=200 green n=200 · 26× p=503 n=200 · 6× p=200 blue n=200
nextgen: unchanged
```

## Rollback

```console
$ curl -X PUT …/written-on-green
$ dk staging rollback --yes
  site: bin/snapshot-db staging restore …/snapshots/20261127T162147Z.dump
  site: bin/switch-site staging blue 20030
blue is live again.
{"colour": "blue", "units": "1", "notes": ["first-note"], …}      (the note written on green is gone)
probe: 40× p=200 green · 21× p=503 · 2× p=200 blue; nextgen 200 throughout, unchanged
```

## A release whose migration breaks the database puts everything back

`v2-broken`'s migration drops `notes` and then fails:

```console
$ dk staging deploy                       # green live; blue gets v2-broken
  site: bin/snapshot-db staging freeze
blue didn't start (failed: notes-migrate@staging-blue.service).
    migration failed after dropping notes
  site: bin/snapshot-db staging thaw
  site: bin/switch-site staging green 20040
Put back: green is live again, with the database as it was.
{"colour": "green", "notes": ["first-note", "second-note"], …}
probe: 48× p=200 green · 34× p=503 · 3× p=200 green; nextgen 200 throughout, unchanged
```

## Working on Nextgen touches nothing of Partisipa

```console
$ (nextgen) dk staging db restore --file nextgen.dump --yes
db restore: done.
probe: partisipa 200 throughout (nextgen 502 while it restored)
partisipa: unchanged (every container's start time, in site, blue and green)

$ (nextgen) dk staging stop      →  nextgen: 502, partisipa: 200, partisipa unchanged
$ (nextgen) dk staging start     →  nextgen: 200
```

## Accounts exist only from the first boot

`host.yaml` was given an account `training` (slot 5), and Nextgen an env `training`, without
building a new server:

```console
$ dk host shared-1 check
shared-1/generated differs from a fresh render (run `dk host shared-1 render` and commit):
$ (nextgen) dk training secrets push
dk training secrets push: shared-vm has no account training: accounts are made only at a server's
first boot (host.yaml of shared-1), never added to a running one. Build a new server with it
$ (nextgen) dk training
dk training: shared-vm has no account training: …
$ getent passwd training
no account training on the server
```

## The firewall between projects

```console
green → 169.254.1.3:20021 (the site's database): connected
deploy (nextgen) → the same port: refused
```

The first server also had the edge's Caddy admin API listening on `127.0.0.1:2019`, reachable
from any account given the host's loopback. The edge now runs with its admin API off. The
final server, built from the final Ignition:

```console
== loopback listeners
127.0.0.1:20010 127.0.0.1:20020 127.0.0.1:20021 127.0.0.1:20040
```

## A reboot

The first reboot found a gap, since fixed. dk had not enabled the site's units at boot, and a
colour started before the site's database existed. Now the site's target is enabled, and the
live colour starts through `dk-colour-boot.service`, which waits until the site's ports
answer:

```console
$ sudo systemctl reboot
deploy: db web caddy
site: notes-staging-site-infra notes-staging-db notes-staging-front
blue: notes-staging-blue-infra notes-staging-blue-web          (live)
green:                                                         (idle: not started)
partisipa: 200   nextgen: 200   notes: ["first-note", "second-note"]
```

## Memory, all processes per account

```console
deploy 59 MB   site 98 MB   blue 26 MB (idle)   green 81 MB (live)   edge 69 MB
```

## Not tested here

- Backups and Zabbix on a shared server.
- `dk host` creating a DigitalOcean droplet.
- Partisipa's real units and images.
- A server holding two blue/green sites.
