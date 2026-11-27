# bluegreen-app: a Partisipa-shaped blue/green site

A test app for dk's blue/green deploy, in the shape of Partisipa's units
(partisipa-import `infra/quadlets`), with made-up data:

- **The site** (the site account): `notes-site@.pod` with its database (`notes-db@`) and
  its front (`notes-front@`, a Caddy the switch points at one colour). Started by
  `notes-site@<env>.target`.
- **A colour** (the blue or green account): `notes@.pod` with a migrate oneshot
  (`notes-migrate@`) and the web (`notes-web@`). Started by `notes@<env>-<colour>.target`.
- **As Partisipa's units do,** the pods name `Network=edge-net.network`, which dk's drop-in
  resets to the host-loopback network, and the units mount their own secrets
  (`notes-<instance>-<role>`); dk only creates them.
- **`bin/`**: the hooks, miniatures of Partisipa's `snapshot-db` and `switch-site`, run in
  the site account. `app.toml` says which unit belongs where, the ports, and the hooks.

Three versions of the units folder:

| Folder | What it tests |
|---|---|
| `v1/` | units version 1 |
| `v2/` | units version 2: the same template names, with different content |
| `v2-broken/` | v2 whose migration drops the `notes` table and then fails, so a deploy has to put the database back |

`app/` is the image, `notes-bluegreen:1` in the spike registry (`10.0.2.2:15000` from the
VM). `tests/test_bluegreen_fixture.py` checks the hooks against a fake `podman` and `curl`.
