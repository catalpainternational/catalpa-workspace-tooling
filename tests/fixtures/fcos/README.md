# `dk <env> fcos render` golden fixtures

Synthetic projects, each in the shape of a real one (the real repositories are private):

- `site/`: catalpa-site. One-shot `migrate` and `collectstatic`, a healthchecked db, Caddy
  mounting app volumes read-only; no `ops:`, so `fcos.bind_volumes` and pinned `id_ranges`.
- `bero/`: the Bero family. A root compose file that `include`s a shared one plus a
  required-secrets overlay with an `env_file`; Redis, RQ workers and Metabase; Caddy mounting
  app volumes writable (so it shares the app's ID range); `auto_update` on.
- `swlt/`: swlt. An edge image whose entrypoint wraps Caddy and reads a credential at runtime;
  mailpit with published ports dropped; extra restic data volumes; a credential with a compose
  default (which `fcos check` reports).

`expected/` holds each render. After an intended change, regenerate them with
`UPDATE_GOLDEN=1 uv run pytest tests/test_fcos_render.py` and review the diff.
