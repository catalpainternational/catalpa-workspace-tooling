# Running `dk` on rootless Podman (Linux)

`dk` calls `docker` and `docker compose` by name. On Linux you can point those at rootless
Podman and run a project's local stacks without Docker installed. This was checked with
catalpa-site `main` (295ec575, tooling v1.6.0) on Fedora 45, using Podman 6.1.4, the static
docker CLI 29.9.2 and Docker Compose v5.5.1. Both `dk dev up -d` (runserver + webpack, hot
reload) and `dk full up -d` (gunicorn + built static) served all three sites, empty and
then with prod data restored (all 349 live pages answered 200 in both).

macOS is not covered: under `podman machine` the VM mount does not pass on file-change
events, so hot reload silently stops working (#94). Remote deploys (`DOCKER_HOST=ssh://…`)
and `dk push` were not tried.

## 1. One-time machine setup

**Podman API socket.** Compose talks to Podman through its Docker-compatible socket:

```bash
systemctl --user enable --now podman.socket
curl -s --unix-socket "$XDG_RUNTIME_DIR/podman/podman.sock" http://d/version | head -c 80
```

If the unit says `active (listening)` but `$XDG_RUNTIME_DIR/podman/podman.sock` is missing,
run `systemctl --user restart podman.socket`; the unit does not notice the file being
deleted.

**docker CLI and compose plugin.** The docker CLI is only a client here; no Docker daemon is
installed. The static build installs without root:

```bash
curl -fsSLO https://download.docker.com/linux/static/stable/x86_64/docker-29.9.2.tgz
tar xzf docker-29.9.2.tgz docker/docker
install -m755 docker/docker ~/.local/bin/docker
mkdir -p ~/.docker/cli-plugins
ln -sf "$(command -v docker-compose)" ~/.docker/cli-plugins/docker-compose  # compose v2+

docker context create podman --docker "host=unix://$XDG_RUNTIME_DIR/podman/podman.sock"
docker context use podman
docker version --format '{{.Client.Version}} / server {{.Server.Version}}'
```

`podman-docker` (the `docker` → `podman` shim) is the alternative. It was not tried here,
and the real CLI keeps `docker compose config --format json` and the compose labels exactly
as dk expects them.

**Low ports.** The local proxy publishes 80 and 443 by default, and rootless Podman can only
bind them after:

```bash
sudo sysctl net.ipv4.ip_unprivileged_port_start=80
echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/50-unprivileged-ports.conf
```

If you'd rather not grant that, remap the proxy's host ports instead (`CATALPA_LOCAL_PROXY_HTTP_PORT`
/ `CATALPA_LOCAL_PROXY_HTTPS_PORT`, section 3) to ports above 1024.

## 2. SELinux (Fedora, RHEL and derivatives)

With SELinux enforcing, a container cannot read a host folder that is mounted without a
label. This applies to Docker on Fedora as well as to Podman. Check with `getenforce`.

**dk's own containers.** The local proxy and dk's one-off helpers (media pull and push, the
SBOM and ORAS containers, restic with a DC backup CA) bind-mount host paths. When
`docker info` reports SELinux, dk starts them with `--security-opt label=disable`, which
leaves host labels alone and lifts SELinux confinement for that container only. It does not
use `:z`, because that would permanently relabel paths such as the Caddyfile inside `.venv`
(lost on every `uv sync`), an extract folder you chose, or the Docker socket. Set
`DK_SELINUX_LABEL_DISABLE=1` to force the flag if detection misses, or `=0` to turn it off.

A `catalpa-local-proxy` that an older dk created without the flag is recreated on the next
`dk <env> up` or `dk proxy up`; its CA is kept. If the proxy still exits straight after
starting, dk now stops and prints the exit code and `docker logs`, instead of carrying on.

**Project compose files.** Bind mounts in a project's own compose files are not dk's to
change. Without a label, dev stack services get `Permission denied` on mounts such as
`./catalpa_wagtail`, `./media` and `./docker/caddy/Caddyfile`. Add `:z` (shared label) to
each host-path bind mount in the consumer repo:

```yaml
    volumes:
      - ./catalpa_wagtail:/app/catalpa_wagtail:z
      - ./media:/app/media:z
      - ./docker/caddy/Caddyfile:/etc/caddy/Caddyfile:ro,z
```

`:z` is ignored where SELinux is off, including Docker Desktop on macOS. Use `:z`, not `:Z`
(private label), for anything two services mount. Named volumes need nothing. Until a
project has this, label the paths once by hand, which is what `:z` does:

```bash
chcon -R -t container_file_t catalpa_wagtail media docker/caddy/Caddyfile   # catalpa-site paths
```

New files created inside those folders inherit the label. `restorecon -R <path>` undoes it.

## 3. When something else already holds port 443

By default dk's proxy publishes `80:80` and `443:443` on every address. If another reverse
proxy on the machine already holds 443, even on loopback only, `dk <env> up` stops with:

```
docker: Error response from daemon: rootlessport listen tcp :443: bind: address already in use
```

`ss -ltnp 'sport = :443'` shows who holds the port. The stacks themselves are created
before this point, but nothing routes to them.

**Preferred fix: configure the proxy's host ports.** Three machine-wide env vars control
this (set them in your shell profile, then `dk proxy down && dk <env> up` to recreate the
container — an already-created container keeps the ports it was created with):

- `CATALPA_LOCAL_PROXY_HTTP_PORT` / `CATALPA_LOCAL_PROXY_HTTPS_PORT` — remap the published
  host port, e.g. `CATALPA_LOCAL_PROXY_HTTPS_PORT=8443`. Set to `0` (or `off`) to skip
  publishing that port at all.
- `CATALPA_LOCAL_PROXY_BIND` — bind address for both published ports, e.g. `127.0.0.1` to
  keep them off the LAN.

Printed `Local dev URL:` lines include the configured HTTPS port automatically when it
isn't 443. If you disable the HTTP port, LAN CA auto-download (`dk proxy ca`) says so
instead of printing a broken URL — copy the CA file from `~/.config/catalpa/local-proxy/`
to the device instead.

**Alternative: chain behind the existing proxy.** If you'd rather route from the existing
proxy to dk's (e.g. because it needs to keep serving 443 itself), set
`CATALPA_LOCAL_PROXY_HTTP_PORT=0 CATALPA_LOCAL_PROXY_HTTPS_PORT=0` so dk's proxy joins only
the shared Docker network with no host ports, then connect it to the front proxy's network:

```bash
dk proxy down
dk <env> up
podman network connect <front-proxy-network> catalpa-local-proxy
```

Then route `*.localdev.temp.build` from the front proxy to it. For a Caddy front proxy:

```caddy
*.localdev.temp.build {
	tls internal
	reverse_proxy https://catalpa-local-proxy:443 {
		header_up Host {host}
		transport http {
			tls_server_name {host}
			tls_insecure_skip_verify
		}
	}
}
```

`header_up Host {host}` is required. Without it, Caddy sends the upstream's own address as
the Host header, dk's proxy matches none of its routes, and every page comes back as an
empty `200`. The certificate in the browser is then the front proxy's, so trust that CA
instead of running `dk proxy trust`.

`dk proxy down` removes the container, and the next `dk <env> up` recreates it on
`catalpa-local-proxy-net` only, so re-run `podman network connect` after that. The
container has no restart policy, so after a reboot start it with
`docker start catalpa-local-proxy` (or run `dk <env> up`).

## 4. Production data

Taking a database dump and the media from prod works under Podman as it does under Docker,
because `DOCKER_HOST=ssh://…` sends those commands to the prod host's own Docker. For
catalpa-site, against a 19.6 MB dump and 1.8 GB of media (8,609 files):

```bash
dk prod db pgdump > docker/postgres/dumps/catalpa_db.custom      # 9 s
dk fetch media --no-legacy-path                                  # 2 min 20 s
dk dev  db restore --file docker/postgres/dumps/catalpa_db.custom   # 32 s
dk full db restore --file docker/postgres/dumps/catalpa_db.custom   # 17 s
dk full files push --yes                                         # 14 s, into the Podman volume
```

Files that rsync adds under an SELinux-labelled `media/` take that label too, so no further
`chcon` is needed. One other route stops on a problem unrelated to Podman:

- **`native fetch db` and `dk fetch db` (catalpa-site).** Both fall back to the old fetch
  script, which connects to `native.fetch_media.legacy.ssh_host`. For catalpa-site that host
  (`site-production.catalpa.build`) no longer resolves. dk then waits 2 minutes and reports
  that SSH "may still be starting on the new droplet". `dk prod db pgdump` reads from the
  `docker_host` in the prod `info.yaml` instead.

## 5. Check it works

```bash
dk dev up -d
curl -s -o /dev/null -w '%{http_code} %{size_download}\n' https://catalpa-site-dev.localdev.temp.build/
dk dev ps
dk dev manage showmigrations wagtailcore | tail -1
```

Expect `200` with a non-empty body. An empty `200` means the request reached a Caddy with no
route for that hostname (see section 3). To check hot reload, `touch` a Python file and a
`.scss` file. The django log should show `… changed, reloading.` and the webpack log
`compiled successfully`.

## 6. Performance

Measured on this setup with prod data loaded, warm, median of 15 requests through all
three proxies (edge, dk's proxy, the stack's Caddy):

| page | dev (runserver + debugpy) | full (gunicorn) |
| --- | --- | --- |
| homepage | 154 ms | 118 ms |
| `/careers/` | 87 ms | 50 ms |
| `/admin/login/` | 58 ms | 18 ms |
| one image | 10 ms | 6 ms |

Nothing here is slow because of Podman. Files on bind mounts send real change events, so
webpack rebuilds on save and sits at 0.05% CPU while idle. The host is amd64, so builds
need no emulation, and a cold `dk full up -d` takes about 3 minutes.

The one idle cost belongs to the project. Without a file-watching library installed,
Django's runserver checks every watched file once a second. For catalpa-site that is 3,099
files, 95% of them template and translation folders inside installed packages, and each
check takes about 90 ms. So dev Django uses about 9–11% of a core while doing nothing, on
any engine, and once more for each worktree stack. A watcher that uses change events
(`pywatchman` with watchman, or `django-watchfiles`) would remove that cost on Linux. On
macOS with Podman, though, Django would then stop reloading, because those events do not
cross the VM boundary (#94). So switch it on per machine, not for everyone.

## Known gaps

- Project compose files need `:z` on host-path bind mounts under SELinux; dk can't add it
  for them (section 2).
- Against Podman's compat API, compose drops `platform:` and builds native. `dk push` checks
  the built architecture since v1.6.0 (#114).
