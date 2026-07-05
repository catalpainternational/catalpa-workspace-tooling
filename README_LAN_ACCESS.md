# LAN access for local dev (phones/tablets on the same Wi-Fi)

Reach your local dev HTTPS URLs from other devices on the same network (phones, tablets, a second laptop) using magic DNS under `*.lan.localdev.temp.build`, served by the same machine-wide dev proxy.

**Related docs**

- [README_DEV_PROXY.md](README_DEV_PROXY.md) — the underlying local dev HTTPS proxy, CA trust, and `local_proxy` configuration

## Enable

Set `local_proxy.lan_access: true` (or top-level `dev_lan_access: true`) in `docker/envs/<env>/info.yaml`:

```yaml
site_origin: https://myapp-dev.localdev.temp.build
local_proxy:
  enabled: true
  lan_access: true
env:
  compose_project_name: myapp_dev
```

Tooling detects the host's LAN IPv4 and registers extra proxy routes, e.g. `https://myapp-dev.192-168-1-42.lan.localdev.temp.build` → same upstream as the desktop hostname.

## Trust the CA on each device

Devices must trust the local dev CA once. Run `dk proxy ca` for:

- a CA download URL (served over plain HTTP at `/catalpa-local-ca.crt`),
- a terminal QR code, and
- per-OS install steps (iOS/Android).

See [README_DEV_PROXY.md](README_DEV_PROXY.md#ca-trust) for how the CA is named and persisted.

## Optional: alternate DNS suffix

Default suffix is `lan.localdev.temp.build` (NS delegated to [sslip.io](https://sslip.io/) nameservers). Override per project in `tooling.yaml`:

```yaml
dev:
  lan_dns_suffix: sslip.io
```

Or per env in `info.yaml`:

```yaml
local_proxy:
  lan_access: true
  lan_dns_suffix: sslip.io
```

Use `sslip.io` when you do not control `lan.localdev.temp.build`. Suffixes outside `.localdev.temp.build` require `VITE_EXTRA_ALLOWED_HOSTS` injection for Vite-based frontends.
