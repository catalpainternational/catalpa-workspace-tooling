# LAN access for local dev (phones/tablets on the same Wi-Fi)

Reach your local dev HTTPS URLs from other devices on the same network (phones, tablets, a second laptop) using [sslip.io](https://sslip.io/) magic DNS, served by the same machine-wide dev proxy.

**Related docs**

- [README_DEV_PROXY.md](README_DEV_PROXY.md) — the underlying local dev HTTPS proxy, CA trust, and `local_proxy` configuration

## Enable

Set `local_proxy.lan_access: true` (or top-level `dev_lan_access: true`) in `docker/envs/<env>/info.yaml`:

```yaml
site_origin: https://myapp-dev.localdev.temp.build
local_proxy:
  enabled: true
  service: node
  upstream_port: 5555
  lan_access: true
env:
  compose_project_name: myapp
```

Tooling detects the host's LAN IPv4 and registers extra proxy routes using sslip.io magic DNS, e.g. `https://myapp-dev.192-168-1-42.sslip.io` → same upstream as the desktop hostname.

## Trust the CA on each device

Devices must trust the local dev CA once. Run `dk proxy ca` for:

- a CA download URL (served over plain HTTP at `/catalpa-local-ca.crt`),
- a terminal QR code, and
- per-OS install steps (iOS/Android).

See [README_DEV_PROXY.md](README_DEV_PROXY.md#ca-trust) for how the CA is named and persisted.

## Optional: white-label DNS suffix

Use a custom suffix instead of `*.sslip.io`:

```yaml
local_proxy:
  lan_dns_suffix: lan.localdev.temp.build
```

This requires NS delegation to sslip.io nameservers in your DNS zone. Inject `VITE_EXTRA_ALLOWED_HOSTS` when the suffix is not under `.localdev.temp.build`.
