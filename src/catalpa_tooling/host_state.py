"""The machine behind an env, read over SSH: what ``dk <env> host`` shows after the cloud checks.

``dk <env> host`` used to stop at DigitalOcean (the droplet exists, DNS points at it). The
questions after an OS update window are about the machine itself: did it update, did it come
back, is anything failed. Everything here is read-only.

- A rendered host (``runtime: quadlet``): OS release and a staged update (``rpm-ostree``),
  Zincati, the last boots, failed system units, the data volume mount, and the stack account's
  failed units and linger. System state needs root, so it goes through ``core`` (sudo); the
  account's own state goes through the account.
- A compose host: uptime, whether a reboot is pending, failed units, disk use on ``/``.

Each check is a :class:`Check`; the parsers take the command's text, so they run without a host.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

from catalpa_tooling.fcos.host import Host, HostError

OK, WARN, FAIL = "ok", "warn", "FAIL"


@dataclass(frozen=True)
class Check:
    level: str  # OK, WARN or FAIL
    label: str
    detail: str


def _section(text: str, name: str) -> str:
    """The lines a script printed under ``== name``, up to the next marker."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("== "):
            inside = line[3:].strip() == name
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def parse_ostree(text: str) -> list[Check]:
    """The booted OS version and a staged update, from ``rpm-ostree status --json``."""
    try:
        deployments = json.loads(text).get("deployments") or []
    except (ValueError, AttributeError):
        first = text.splitlines()[0] if text else "no output"
        return [Check(WARN, "OS", f"rpm-ostree status unreadable: {first}")]
    booted = next((d for d in deployments if d.get("booted")), None)
    staged = next((d for d in deployments if d.get("staged")), None)
    out = []
    if booted:
        out.append(Check(OK, "OS", f"booted {booted.get('version') or booted.get('checksum', '?')[:12]}"))
    else:
        out.append(Check(WARN, "OS", "no booted deployment reported"))
    if staged:
        out.append(
            Check(WARN, "update", f"{staged.get('version') or 'a new deployment'} is staged; a reboot applies it")
        )
    else:
        out.append(Check(OK, "update", "nothing staged"))
    return out


def parse_zincati(text: str) -> Check:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return Check(WARN, "zincati", "no output")
    state, recent = lines[0].strip(), lines[1:]
    detail = state + (f"; last: {recent[-1].strip()}" if recent else "")
    return Check(OK if state == "active" else WARN, "zincati", detail)


def parse_boots(text: str) -> Check:
    lines = [" ".join(ln.split()) for ln in text.splitlines() if ln.strip()]
    if not lines:
        return Check(WARN, "boots", "journal has no boot list (not readable?)")
    return Check(OK, "boots", f"{len(lines)} recent; latest: {lines[-1]}")


def parse_failed(text: str, label: str) -> Check:
    names = [ln.split()[0].lstrip("●*") or ln.split()[1] for ln in text.splitlines() if ln.strip()]
    if not names:
        return Check(OK, label, "none")
    return Check(FAIL, label, ", ".join(names))


def parse_mount(text: str, path: str) -> Check:
    mounted = text.strip() == "mounted"
    return Check(OK if mounted else FAIL, "data volume", f"{path} " + ("mounted" if mounted else "is NOT mounted"))


def parse_linger(text: str, user: str) -> Check:
    on = text.strip().lower().endswith("=yes")
    return Check(OK if on else FAIL, "linger", f"{user}: {'on' if on else 'off (the stack stops at logout)'}")


def data_mount_of(manifest: dict) -> str | None:
    """The directory the bind volumes live under (``/var/mnt/data``), from the render manifest."""
    paths = [v["path"] for v in (manifest.get("bind_volumes") or {}).values() if v.get("path")]
    if not paths:
        return None
    return paths[0].rsplit("/", 1)[0]


def rendered_root_script(user: str, data_mount: str | None) -> str:
    mount = (
        f"echo '== mount'; mountpoint -q {data_mount} && echo mounted || echo not-mounted\n"
        if data_mount
        else ""
    )
    return (
        "echo '== ostree'; rpm-ostree status --json 2>&1\n"
        "echo '== zincati'; systemctl is-active zincati.service 2>&1; "
        "journalctl -u zincati.service -n 3 --no-pager -o cat 2>&1\n"
        "echo '== boots'; journalctl --list-boots --no-pager 2>&1 | tail -3\n"
        "echo '== failed'; systemctl --failed --no-legend --plain 2>&1\n"
        f"{mount}"
        f"echo '== linger'; loginctl show-user {user} -p Linger 2>&1\n"
    )


def rendered_state(host: Host, *, user: str, data_mount: str | None) -> list[Check]:
    """State of a rendered host. ``core`` reads the system's; the account its own units."""
    out: list[Check] = []
    try:
        text = host.root_script(rendered_root_script(user, data_mount))
    except HostError as exc:
        out.append(Check(FAIL, "machine", f"can't read the OS as core (an admin key is needed): {_first_line(exc)}"))
    else:
        out += parse_ostree(_section(text, "ostree"))
        out.append(parse_zincati(_section(text, "zincati")))
        out.append(parse_boots(_section(text, "boots")))
        out.append(parse_failed(_section(text, "failed"), "failed system units"))
        if data_mount:
            out.append(parse_mount(_section(text, "mount"), data_mount))
        out.append(parse_linger(_section(text, "linger"), user))
    try:
        r = host.ssh(user, "systemctl --user --failed --no-legend --plain", check=False)
        if r.returncode == 0:
            out.append(parse_failed(r.stdout.decode(errors="replace"), f"failed units ({user})"))
        else:
            err = (r.stderr or b"").decode(errors="replace").strip()
            out.append(Check(FAIL, f"failed units ({user})", f"ssh {user} exited {r.returncode}: {err.splitlines()[0] if err else ''}"))
    except HostError as exc:
        out.append(Check(FAIL, f"failed units ({user})", _first_line(exc)))
    return out


COMPOSE_SCRIPT = (
    "echo '== uptime'; uptime -p 2>&1\n"
    "echo '== reboot'; [ -f /var/run/reboot-required ] && echo required || echo no\n"
    "echo '== failed'; systemctl --failed --no-legend --plain 2>&1\n"
    "echo '== disk'; df -P / 2>&1 | tail -1\n"
)


def parse_disk(text: str) -> Check:
    parts = text.split()
    try:
        used = int(parts[4].rstrip("%"))
    except (IndexError, ValueError):
        return Check(WARN, "disk /", f"unreadable: {text or 'no output'}")
    level = FAIL if used >= 95 else WARN if used >= 85 else OK
    return Check(level, "disk /", f"{used}% used")


def compose_state(host: Host, *, login: str) -> list[Check]:
    try:
        text = host.ssh(login, "sh -s", input=COMPOSE_SCRIPT.encode()).stdout.decode(errors="replace")
    except HostError as exc:
        return [Check(FAIL, "machine", _first_line(exc))]
    reboot = _section(text, "reboot") == "required"
    return [
        Check(OK, "uptime", _section(text, "uptime") or "unknown"),
        Check(WARN if reboot else OK, "reboot", "required (a package update wants one)" if reboot else "not pending"),
        parse_failed(_section(text, "failed"), "failed system units"),
        parse_disk(_section(text, "disk")),
    ]


def _first_line(exc: Exception) -> str:
    return (str(exc).splitlines() or [""])[0]


def docker_host_login(docker_host: str) -> str | None:
    return urlparse(docker_host).username or None


def print_state(env_name: str, address: str, checks: list[Check]) -> int:
    """Print the checks; 1 when any failed, 0 otherwise (a staged update or pending reboot is a warning)."""
    print(f"\nmachine ({env_name}, {address}):", file=sys.stderr)
    width = max(len(c.label) for c in checks)
    for c in checks:
        print(f"  [{c.level:>4}] {c.label:<{width}}  {c.detail}", file=sys.stderr)
    return 1 if any(c.level == FAIL for c in checks) else 0


def show_machine_state(config, env_name: str, info: dict) -> int:
    """``dk <env> host``'s SSH section; 0 when nothing failed."""
    from catalpa_tooling.fcos.host import host_from_docker_host, load_committed
    from catalpa_tooling.fcos.render import generated_dir
    from catalpa_tooling.fcos.settings import runtime_of

    docker_host = str(info.get("docker_host") or "").strip()
    if not docker_host.startswith("ssh://"):
        return 0  # a local env has no machine to look at
    try:
        host = host_from_docker_host(docker_host, None)
    except HostError as exc:
        return print_state(env_name, "?", [Check(FAIL, "machine", str(exc))])
    if runtime_of(info) == "quadlet":
        try:
            committed = load_committed(generated_dir(config, env_name))
        except (OSError, ValueError, KeyError) as exc:
            msg = f"no committed render to read the account from ({exc}); run `dk {env_name} render`"
            return print_state(env_name, host.address, [Check(FAIL, "machine", msg)])
        checks = rendered_state(host, user=committed.user, data_mount=data_mount_of(committed.manifest))
    else:
        login = docker_host_login(docker_host)
        if not login:
            msg = "docker_host has no user (ssh://user@host)"
            return print_state(env_name, host.address, [Check(FAIL, "machine", msg)])
        checks = compose_state(host, login=login)
    return print_state(env_name, host.address, checks)
