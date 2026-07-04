# Local dev HTTPS proxy (`local_proxy` in `info.yaml`)

For local Docker environments (no `docker_host`), projects may enable a **machine-wide** Caddy reverse proxy that maps a real HTTPS hostname under `*.localdev.temp.build` to the stack's front container over a **shared Docker network** (`catalpa-local-proxy-net`). DNS for `*.localdev.temp.build → 127.0.0.1` is org-wide; Caddy uses an **internal CA** (`tls internal`) — trust it once per machine with `dk <env> trust-caddy-cert` or `dk proxy trust`.

**Related docs**

- [README_LAN_ACCESS.md](README_LAN_ACCESS.md) — reach these dev URLs from phones/tablets on the same Wi-Fi via sslip.io magic DNS

## How it works

On `dk <env> up`, tooling creates/joins the shared network, writes a generated compose override (attaches the front service with a project-unique alias and `ports: !reset []`), ensures `catalpa-local-proxy` is running, and registers route(s) via Caddy's admin API (`127.0.0.1:2019`). On `dk <env> down`, that env's route(s) are removed (the shared proxy keeps running for other projects). Manage the proxy directly: `dk proxy up|down|status|trust|ca`.

**Requires Docker Compose 2.24+** for `ports: !reset []` in the generated override.

## CA trust

The CA root is **persisted on the host** at `${XDG_CONFIG_HOME:-~/.config}/catalpa/local-proxy` (bind-mounted at `/data`) and named **`Catalpa Local Dev Root (<machine>)`** in the OS trust store — where `<machine>` is derived from the host's short hostname (override with the `CATALPA_LOCAL_DEV_MACHINE` env var). Each machine self-generates its **own** root key, so the name identifies whose CA it is and trusting one developer's CA does **not** transfer to another's. It is minted **once per machine** and survives `docker volume prune`, proxy re-creation, and reboots — trust once and forget it.

To reset it (forces a one-time re-trust), remove that directory and recreate the proxy:

```bash
rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/catalpa/local-proxy"
dk proxy down && dk proxy up && dk proxy trust
```

## Configuration

Example (`docker/envs/dev/info.yaml`):

```yaml
site_origin: https://myapp-dev.localdev.temp.build
local_proxy:
  enabled: true
  service: node          # compose service that receives proxy traffic
  upstream_port: 5555    # container-internal port (not a host publish)
  lan_access: true       # optional: sslip.io hostnames for LAN devices (see README_LAN_ACCESS.md)
env:
  compose_project_name: myapp
```

The proxy dials `{compose_project_name}-{service}` on the shared network (e.g. `myapp-node:5555`). Override with `upstream_host` if needed.

Multiple hostnames (e.g. app + Metabase) via an explicit `routes` list — `host` defaults to `site_origin` when omitted; all routes share `service` unless a route sets its own:

```yaml
site_origin: https://myapp-full.localdev.temp.build
local_proxy:
  enabled: true
  service: caddy
  routes:
    - upstream_port: 80
    - host: metabase.myapp-full.localdev.temp.build
      upstream_port: 80
env:
  compose_project_name: myapp-full
```

## Status

`dk proxy status` shows whether the proxy is running and lists live sites, for example:

```
catalpa-local-proxy: running (abc123def456)
admin: http://127.0.0.1:2019
live sites:
  myapp:
    dev:
      local:
        myapp-dev.localdev.temp.build -> myapp-node:5555
      lan:
        myapp-dev.192-168-1-42.sslip.io -> myapp-node:5555
    full:
      local:
        myapp-full.localdev.temp.build -> myapp-full-caddy:80
```

LAN magic-DNS routes are grouped under the same project/env as the canonical hostname, in a separate `lan:` section.

## Project requirements when enabled

Set `site_origin` to the HTTPS hostname (for Django `ALLOWED_HOSTS` / CSRF), set `local_proxy.service` to the compose front service, set `upstream_port` to that service's **internal** listen port, and allow those hosts in frontend dev config (e.g. Vite `server.allowedHosts`). Stacks with their own Caddy may run behind the proxy by serving plain HTTP internally (`CADDY_SITE_ADDRESS: http://...`) while keeping `site_origin` as `https://...`. Staging/prod are unchanged — the override is only applied when `local_proxy.enabled` is true on a local Docker host.
