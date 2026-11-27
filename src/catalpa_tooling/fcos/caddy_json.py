"""The edge's Caddy config for a socket-activated rootless host.

Rootless Podman's ``PublishPort`` hides visitors' addresses (connections arrive from Caddy's own
container address), so on FCOS the account's systemd opens 80/443 (``<proxy>.socket``) and
passes them to the container as fd 3 (TCP 80), fd 4 (TCP 443) and fd 5 (UDP 443, HTTP/3).

The project's Caddyfile is left as it is, so plain ``docker compose up`` keeps working. Its JSON
form comes from ``caddy adapt`` run in the edge image itself (same Caddy version and plugins),
with the listeners then pointed at the inherited sockets. Tested on fcos-rootless (2026-11-19):
HTTPS, the HTTP->HTTPS redirect, certificate reuse and visitors' real addresses.
"""

from __future__ import annotations

import json
import shutil

from catalpa_tooling.run_cmd import run as run_cmd

HTTP_FD = "fd/3"
HTTPS_FDS = ("fd/4", "fdgram/5")
REDIRECT_SERVER = "fd_http_redirect"


class CaddyJsonError(RuntimeError):
    """The Caddyfile could not be adapted, or listens where a socket cannot be passed."""


def _port_of(addr: str) -> str:
    return addr.rsplit(":", 1)[-1]


def _redirect_route() -> dict:
    """308 to the same host and path over HTTPS (Caddy's own redirect, which binds :80 itself)."""
    return {
        "handle": [
            {
                "handler": "static_response",
                "status_code": 308,
                "headers": {"Location": ["https://{http.request.host}{http.request.uri}"]},
            }
        ]
    }


#: Behind the shared edge, the edge connects from the host's loopback: trust its X-Forwarded-For.
LOOPBACK_PROXIES = {"source": "static", "ranges": ["127.0.0.1/32", "::1/128"]}


def rewrite_for_shared_edge(config: dict) -> dict:
    """Behind a shared server's edge: TLS ends there, so the env's one server takes plain HTTP
    on ``fd/3`` (its account's loopback port), whatever port its Caddyfile names, and trusts the
    edge's forwarded client address."""
    servers = config.setdefault("apps", {}).setdefault("http", {}).setdefault("servers", {})
    if len(servers) != 1:
        raise CaddyJsonError(
            f"behind a shared server's edge the env's Caddy has one server; this one has {sorted(servers)}"
        )
    for srv in servers.values():
        srv["listen"] = [HTTP_FD]
        srv["listen_protocols"] = [["h1"]]
        srv["automatic_https"] = {"disable": True}
        srv.pop("tls_connection_policies", None)
        srv["trusted_proxies"] = dict(LOOPBACK_PROXIES)
    config["apps"].pop("tls", None)
    return config


def loopback_socket_unit(description: str, port: int) -> str:
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "\n"
        "[Socket]\n"
        "# Passed to the container as fd 3 (see caddy.json); the shared edge connects here.\n"
        f"ListenStream=127.0.0.1:{port}\n"
        "\n"
        "[Install]\n"
        "WantedBy=sockets.target\n"
    )


def rewrite_listeners(config: dict) -> dict:
    """Point every server of an adapted config at the systemd-passed sockets.

    On ``fd/3`` Caddy can't see that a server is port 80's, so it would treat an ``http://`` site
    as HTTPS: serve TLS there and ask for certificates for its names. Such a server gets
    ``automatic_https.disable``, and a last route redirecting what it doesn't match to HTTPS."""
    servers = config.setdefault("apps", {}).setdefault("http", {}).setdefault("servers", {})
    has_http = False
    for name, srv in servers.items():
        ports = {_port_of(a) for a in srv.get("listen", [":443"])}
        if ports - {"80", "443"}:
            raise CaddyJsonError(
                f"Caddy server {name} listens on {sorted(ports)}; only 80 and 443 are passed in"
            )
        if ports == {"443"}:
            srv["listen"] = list(HTTPS_FDS)
            srv["listen_protocols"] = [["h1", "h2"], ["h3"]]
            srv.setdefault("automatic_https", {})["disable_redirects"] = True
        elif ports == {"80"}:
            srv["listen"] = [HTTP_FD]
            srv["listen_protocols"] = [["h1"]]
            srv["automatic_https"] = {"disable": True}
            srv.setdefault("routes", []).append(_redirect_route())
            has_http = True
        else:
            raise CaddyJsonError(f"Caddy server {name} mixes ports {sorted(ports)}")
    if not has_http:
        # Caddy's automatic HTTP->HTTPS redirect binds :80 itself, which it cannot do here, so
        # the redirect is spelled out on the passed socket.
        servers[REDIRECT_SERVER] = {
            "listen": [HTTP_FD],
            "listen_protocols": [["h1"]],
            "routes": [_redirect_route()],
        }
    return config


def adapt_in_image(
    image: str,
    env: dict[str, str],
    *,
    caddyfile: str,
    platform: str | None = None,
) -> tuple[dict, list[str]]:
    """``caddy adapt`` of the image's own Caddyfile with the edge's settings in its environment
    (``{$VAR}`` is substituted at adapt time; credentials arrive as markers, never values), and
    the image's entrypoint. No network; nothing mounted."""
    if not shutil.which("docker"):
        raise CaddyJsonError("adapting the Caddyfile needs `docker` (to run the edge image)")
    cmd = ["docker", "run", "--rm", "--network", "none", "--entrypoint", "caddy"]
    if platform:
        cmd += ["--platform", platform]
    for key in sorted(env):
        cmd += ["-e", f"{key}={env[key]}"]
    cmd += [image, "adapt", "--config", caddyfile, "--adapter", "caddyfile"]
    r = run_cmd(cmd, capture_output=True, text=True, check=False, print_cmd=False)
    if r.returncode != 0:
        raise CaddyJsonError(f"caddy adapt in {image} failed: {(r.stderr or '').strip()[-800:]}")
    try:
        adapted = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise CaddyJsonError(f"caddy adapt in {image} printed no JSON: {exc}") from exc
    r = run_cmd(
        ["docker", "image", "inspect", "--format", "{{json .Config.Entrypoint}}", image],
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode != 0:
        raise CaddyJsonError(f"docker image inspect {image} failed: {(r.stderr or '').strip()}")
    entrypoint = json.loads(r.stdout or "null") or []
    return adapted, [str(w) for w in entrypoint]


def caddy_json_text(adapted: dict, *, shared_edge: bool = False) -> str:
    rewrite = rewrite_for_shared_edge if shared_edge else rewrite_listeners
    return json.dumps(rewrite(adapted), indent=2, sort_keys=True) + "\n"


def socket_unit(description: str) -> str:
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "\n"
        "[Socket]\n"
        "# Passed to the container in this order: fd 3, fd 4, fd 5 (see caddy.json).\n"
        "BindIPv6Only=both\n"
        "ListenStream=[::]:80\n"
        "ListenStream=[::]:443\n"
        "ListenDatagram=[::]:443\n"
        "\n"
        "[Install]\n"
        "WantedBy=sockets.target\n"
    )


def run_adapt(
    image: str, env: dict[str, str], caddyfile: str, platform: str | None
) -> tuple[dict, list[str]]:
    """Default adapter for render (tests pass their own)."""
    return adapt_in_image(image, env, caddyfile=caddyfile, platform=platform)


__all__ = [
    "CaddyJsonError",
    "adapt_in_image",
    "caddy_json_text",
    "rewrite_listeners",
    "run_adapt",
    "socket_unit",
]
