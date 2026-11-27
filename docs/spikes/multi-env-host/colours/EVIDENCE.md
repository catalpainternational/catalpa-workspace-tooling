# Evidence: blue and green in separate accounts (2026-11-27)

The same throwaway Fedora CoreOS VM as `../EVIDENCE.md` (Podman 6.1.1, 2.5 GB), with the earlier
envs stopped. The test app (`app/`) keeps notes in Postgres and files in S3, so a colour has
something to migrate and something to upload. Its media is in S3 because Partisipa's is: no
shared media folder.

| Account | Holds | Reached on the host's loopback at |
|---|---|---|
| `site` | Postgres (`site-db`), SeaweedFS S3 (`site-s3`), the site's Caddy front (`site-front`) | 5433, 8333, 8201 |
| `blue` | `notes-app` (+ `notes-report` and its timer from units version 10) | 8211 |
| `green` | the same file names, at whatever version green was last given | 8212 |
| `edge` | the host edge from `../host-edge/`: `notes.spike.test` → 8201 | 80/443 |

`deploy.sh` runs on the laptop and drives the accounts over SSH, as dk would. It follows
Partisipa's site deploy: install the idle colour, stop the live one, freeze the database
(`CREATE DATABASE app TEMPLATE app_before`), start the new colour, switch the front, and put it
all back if the new colour doesn't answer. `probe.sh` (not committed: a curl through the edge
every 0.2 s) measured outages.

## Adding accounts to a running host

```console
$ time (for a in "site 1021 851968" "blue 1022 917504" "green 1023 983040"; do
    vm core "sudo sh /tmp/add-account.sh $a" < id_spike.pub; done)
site: uid 1021, subuid site:851968:65536, linger, ssh
blue: uid 1022, subuid blue:917504:65536, linger, ssh
green: uid 1023, subuid green:983040:65536, linger, ssh
real	0m1.483s
blue ssh ok; podman 6.1.1;          1     917504      65536
```

`add-account.sh` is 20 lines. `useradd` picks a subordinate range itself, so the script replaces
it with the pinned one.

## Reaching another account's port

Podman's `host.containers.internal` (169.254.1.2) leads to the host's outside address, not
its loopback. It can't reach a port another account published on 127.0.0.1:

```console
blue$ podman run --rm postgres:17-alpine pg_isready -h host.containers.internal -p 5433
host.containers.internal:5433 - no response
blue$ podman run --rm caddy:2-alpine wget -qO- -T3 http://host.containers.internal:8201/
wget: can't connect to remote host (169.254.1.2): Connection refused
```

pasta's `--map-host-loopback` does reach it. Every unit that talks across accounts now has
`Network=pasta:--map-host-loopback,169.254.1.3`:

```console
blue$ podman run --rm --network pasta:--map-host-loopback,169.254.1.3 postgres:17-alpine pg_isready -h 169.254.1.3 -p 5433
169.254.1.3:5433 - accepting connections
```

## Deploys

```console
$ ./deploy.sh units/v9                     # first deploy: blue
22:25:10.218 deploy v9: live=none idle=blue
22:25:13.253 5. blue is live
{"colour": "blue", "app": 1, "units": "9", "columns": ["id", "name"], "notes": [], "x_forwarded_for": "10.0.2.2, 10.0.2.15"}
{"stored": "first.txt", "by": "blue"}

$ ./deploy.sh units/v10                    # green (after the /tmp fix below, again to blue)
22:26:03.094 deploy v10: live=green idle=blue
22:26:14.172 2. green stopped
22:26:14.630 3. database frozen (app_before)
22:26:15.903 4. blue answers
22:26:16.442 5. blue is live
{"colour": "blue", "app": 2, "units": "10", "columns": ["id", "name", "size"], "notes": ["first.txt"], …}
probe:      45 200 green;     10 502 ;      2 200 blue;
```

The probe saw about 2 s of 502s, from the old colour stopping to the switch. The 10 s before
"stopped" are the demo app ignoring SIGTERM (Podman waits, then kills it), while it still
served.

**Each colour runs its own units version**, with plain unit names and no `@` instances. Each
account's colour and port live in its own drop-in, which no release touches:

```console
blue:  Description=notes app (units version 10)
green: Description=notes app (units version 9)
```

**A deploy that fails puts itself back.** Units version 11's migration drops a column, commits
and exits non-zero:

```console
$ ./deploy.sh units/v11
   green now has: notes-app.container notes-report.container notes-report.timer
22:27:15.159 3. database frozen (app_before)
22:27:42.929 4. green does not answer: putting everything back
22:27:46.498 4. back on blue, database as before
{"colour": "blue", "app": 2, "units": "10", "columns": ["id", "name", "size"], "notes": ["first.txt", "second.txt"], …}
probe:      45 200 blue;    140 502 ;      4 200 blue;
```

No data was lost. The 28 s outage is the script's health check waiting for its timeout; a
real deploy would watch the unit fail.

**Downgrade in one account, then roll back:**

```console
$ ./deploy.sh units/v9                     # green: 11 → 9, its report job and timer go
   green now has: notes-app.container
{"stored": "third.txt", "by": "green"}
$ ./deploy.sh rollback
22:28:20.993 rollback: green -> blue, database back to before the last deploy
22:28:33.397 rollback: blue is live
{"colour": "blue", "app": 2, "units": "10", "notes": ["first.txt", "second.txt"], …}
S3 still has third.txt: third
probe:      44 200 green;      9 502 ;      1 200 blue;
```

The rollback drops the note written after the deploy, but its file stays in S3. Partisipa's
rollback today leaves media the same way.

**A timer only one version has**, running in its account:

```console
$ ./deploy.sh units/v10                    # green
   green now has: notes-app.container notes-report.container notes-report.timer
report: green 2 notes
timer: notes-report.service
```

## Memory (all processes per account)

```console
site 123 MB      (Postgres, SeaweedFS, Caddy)
blue 27 MB       (idle)
green 103 MB     (live)
edge 79 MB
```

## Found on the way

- **`/tmp` is shared by every account.** The first v10 deploy unpacked into a fixed
  `/tmp/units`. blue's files from the deploy before were still there and green couldn't remove
  them, so green installed blue's version 9. The script now uses `mktemp -d`. The rule for dk:
  never use a fixed shared path on a multi-account host.
- **Quadlet installs only its own file types.** `podman quadlet install` accepted the `.timer`
  without complaint, but nothing loaded it. Timers go in `~/.config/systemd/user/`, and the
  install tracks them so the next release can remove them.
- **`podman quadlet list` shows every account's `/etc/containers/systemd/users/<uid>/`
  files**, env-a's and the edge's included, though each account's systemd loads only its own
  (checked with `systemctl --user list-unit-files`). dk shouldn't trust that list on a shared
  host.
- **A failed deploy used up the rollback point.** Putting the database back renamed
  `app_before` back, so `rollback` would have had nothing to go to. Partisipa keeps a dump per
  deploy in `snapshots/`, and a real tool must do the same.
- **The database and S3 store are on the host's loopback**, reachable from every account and
  protected only by their passwords.
- **A deploy talks to three accounts.** `deploy.sh` has 21 SSH call sites. Most deploys took
  about 13 s, 10 s of which was the demo app's slow stop.
