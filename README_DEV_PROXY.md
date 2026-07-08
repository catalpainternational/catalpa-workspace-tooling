# Local dev HTTPS proxy (`local_proxy` in `info.yaml`)

For local Docker environments (no `docker_host`), tooling enables a **machine-wide** Caddy reverse proxy that maps real HTTPS hostnames under `*.localdev.temp.build` to each stack's **Caddy front** on port 80 over a **shared Docker network** (`catalpa-local-proxy-net`). DNS for `*.localdev.temp.build → 127.0.0.1` is org-wide; Caddy uses an **internal CA** (`tls internal`) — trust it once per machine with `dk <env> trust-caddy-cert` or `dk proxy trust`.

**Related docs**

- [README_LAN_ACCESS.md](README_LAN_ACCESS.md) — reach these dev URLs from phones/tablets on the same Wi-Fi via sslip.io magic DNS

## How it works

On `dk <env> up`, tooling creates/joins the shared network, writes a generated compose override (attaches the stack **Caddy** service with a project-unique alias and `ports: !reset []`), ensures `catalpa-local-proxy` is running, and registers route(s) via Caddy's admin API (`127.0.0.1:2019`). On `dk <env> down`, that env's route(s) are removed (the shared proxy keeps running for other projects). Manage the proxy directly: `dk proxy up|down|status|trust|ca`.

**Requires Docker Compose 2.24+** for `ports: !reset []` in the generated override.

Every stack must expose **Caddy on container port 80** as the sole front door (`stack.services.proxy`, usually `caddy`). Dev backends (Vite/webpack, Django runserver) stay internal; stack Caddy routes to them.

## CA trust

The CA root is **persisted on the host** at `${XDG_CONFIG_HOME:-~/.config}/catalpa/local-proxy` (bind-mounted at `/data`) and named **`Catalpa Local Dev Root (<machine>)`** in the OS trust store — where `<machine>` is derived from the host's short hostname (override with the `CATALPA_LOCAL_DEV_MACHINE` env var). Each machine self-generates its **own** root key, so the name identifies whose CA it is and trusting one developer's CA does **not** transfer to another's. It is minted **once per machine** and survives `docker volume prune`, proxy re-creation, and reboots — trust once and forget it.

To reset it (forces a one-time re-trust), remove that directory and recreate the proxy:

```bash
rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/catalpa/local-proxy"
dk proxy down && dk proxy up && dk proxy trust
```

## Configuration

The proxy is **on by default** for local Docker envs. Opt out:

```yaml
local_proxy:
  enabled: false
```

### Minimal onboarding

Hostnames follow `{project-slug}-{env}.localdev.temp.build` from `tooling.yaml` `project.name` (underscores → hyphens). Compose project names default to `{stack.compose_project_default}_{env}`.

```yaml
# docker/envs/dev/info.yaml
compose_file: compose.dev.yaml
env:
  CADDY_SITE_ADDRESS: http://ambulancia-dev.localdev.temp.build
```

Tooling derives `site_origin`, registers `{compose_project}_dev-caddy:80`, and injects Caddy/Django env vars on `dk up`.

Explicit primary hostname (optional):

```yaml
site_origin: https://myapp-dev.localdev.temp.build
env:
  CADDY_SITE_ADDRESS: http://myapp-dev.localdev.temp.build
```

### Extra hostnames (admin, stats/Metabase)

```yaml
local_proxy:
  roles: [stats]   # registers stats.{primary-host} → same caddy:80
env:
  CADDY_METABASE_SITE_ADDRESS: http://stats.myapp-full.localdev.temp.build
```

Role labels: `admin`, `stats` (prefix subdomains on the primary hostname).

### LAN (opt-in)

```yaml
local_proxy:
  lan_access: true
```

See [README_LAN_ACCESS.md](README_LAN_ACCESS.md).

## Status

`dk proxy status` shows whether the proxy is running and lists live sites, for example:

```
catalpa-local-proxy: running (abc123def456)
admin: http://127.0.0.1:2019
live sites:
  myapp:
    dev:
      local:
        myapp-dev.localdev.temp.build -> myapp_dev-caddy:80
    full:
      local:
        myapp-full.localdev.temp.build -> myapp_full-caddy:80
        stats.myapp-full.localdev.temp.build -> myapp_full-caddy:80
      lan:
        myapp-full.192-168-1-42.sslip.io -> myapp_full-caddy:80
```

LAN magic-DNS routes are grouped under the same project/env as the canonical hostname, in a separate `lan:` section.

## Project requirements

- **Stack Caddy** on container port **80** (`stack.services.proxy`).
- **`CADDY_*_SITE_ADDRESS`** env vars as plain **`http://…`** (machine proxy terminates TLS).
- Frontend dev config must allow `.localdev.temp.build` hosts (e.g. Vite `server.allowedHosts`).
- Staging/prod are unchanged — the override applies only on local Docker hosts without `docker_host`.

## Deployed envs (staging/prod) — `https://` addresses

For remote envs (`docker_host: ssh://…`, local proxy off), tooling injects the same
`CADDY_*_SITE_ADDRESS` vars but as **`https://` origins** so the stack Caddy provisions its
own certificates and serves HTTPS directly (no machine proxy in front):

- `CADDY_SITE_ADDRESS` ← primary `site_origin`.
- `CADDY_DJANGO_SITE_ADDRESS` ← bero stacks only (`paths.frontend: bero`): from `DJANGO_ORIGIN`
  if set, else derived `admin.{primary-host}`.
- `CADDY_METABASE_SITE_ADDRESS` ← only when Metabase is routed: explicit `METABASE_ORIGIN` /
  `METABASE_SITE_ORIGIN`, a `stats` role, a bero stack with Metabase fetch configured, or a
  second `site_origin` entry.

All values use `setdefault`, so anything set explicitly in `info.yaml` `env:` wins.
