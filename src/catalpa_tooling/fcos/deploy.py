"""A bare ``dk <env>`` on a ``runtime: quadlet`` env: bring the host up to date.

On a compose env a bare ``dk <env>`` runs ``docker compose up -d``. The quadlet equivalent:

1. The committed render must match a fresh one (``dk <env> check``), so only committed files reach
   the host.
2. The host's secrets must all exist: a unit started without one fails.
3. Files that differ are installed, and the units they affect restarted (``dk <env> apply``).
4. Every image an account quadlet runs is pulled. A unit whose image changed is restarted,
   unless step 3 already restarted it. One-shot units (backups) pick the new image up on their
   next run, and ``migrate``/``collectstatic`` re-run with ``django`` (``PartOf=``).

Step 4 is how a new image under the same tag goes out, with ``fcos.auto_update`` off.
"""

from __future__ import annotations

import re
import shlex
import sys

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.fcos import host as h

CHANGED = "CHANGED "


def account_images(c: h.Committed) -> dict[str, list[str]]:
    """Image → the account quadlets that run it."""
    out: dict[str, list[str]] = {}
    for stem, (_dest, text, is_user) in sorted(h._quadlets(c).items()):
        m = re.search(r"^Image=(\S+)", text, re.M)
        if m and is_user:
            out.setdefault(m.group(1), []).append(stem)
    return out


def pull_script(c: h.Committed, images: list[str]) -> str:
    """Pull each image as the account; print ``CHANGED <image>`` when its ID moved."""
    auth = shlex.quote(h.auth_file(c))
    lines = [
        "set -e",
        f'AUTH=; [ -f {auth} ] && AUTH="--authfile {auth}"',
    ]
    for image in images:
        i = shlex.quote(image)
        lines += [
            f"before=$(podman image inspect --format '{{{{.Id}}}}' {i} 2>/dev/null || true)",
            f"podman pull -q $AUTH {i} >/dev/null",
            f"after=$(podman image inspect --format '{{{{.Id}}}}' {i})",
            f'[ "$before" = "$after" ] || echo {shlex.quote(CHANGED + image)}',
        ]
    return "\n".join(lines) + "\n"


def units_for_images(c: h.Committed, changed: list[str]) -> list[str]:
    """The long-running units to restart so they run the ``changed`` images."""
    images = account_images(c)
    units: set[str] = set()
    for image in changed:
        for stem in images.get(image, []):
            unit = h.long_running(c, stem)
            if unit:
                units.add(unit)
    return sorted(units)


def start_what_is_down(target: h.Host, c: h.Committed) -> list[str]:
    """Start the stack's long-running units that aren't active, as compose's ``up -d`` starts
    stopped services. Their ``PartOf=`` one-shots (``migrate``) get ``reset-failed`` first, so
    one that failed earlier runs again. Returns the units it started."""
    user_units, _system = h.stack_units(c)
    if not user_units:
        return []
    r = target.ssh(c.user, "systemctl --user is-active " + " ".join(user_units), check=False)
    states = r.stdout.decode().split()
    down = [u for u, s in zip(user_units, states, strict=False) if s != "active"]
    if not down:
        return []
    related = sorted(
        f"{stem}.service"
        for stem, (_d, _t, is_user) in h._quadlets(c).items()
        if is_user and h.long_running(c, stem) in down
    )
    proxy = c.manifest.get("proxy")
    sockets = [f"{proxy}.socket"] if proxy else []
    q = " ".join(shlex.quote(u) for u in related)
    print(f"{target.address}: systemctl --user start {' '.join(down + sockets)}", file=sys.stderr)
    target.ssh(
        c.user,
        f"systemctl --user reset-failed {q} 2>/dev/null; "
        f"systemctl --user start {' '.join(shlex.quote(u) for u in down + sockets)}",
        check=False,
    )
    return down


def deploy(
    config: ProjectConfig,
    env_name: str,
    info: dict,
    *,
    yes: bool,
    dry_run: bool,
    tag: str | None,
    target: h.Host | None = None,
) -> int:
    from catalpa_tooling.fcos.cli import _check, apply_plan
    from catalpa_tooling.fcos.render import RenderError, generated_dir, render_env

    if tag:
        print(
            f"dk {env_name}: --tag doesn't apply to a quadlet env. Its images follow info.yaml's "
            f"`image_tag`: change it, `dk {env_name} render`, commit, then `dk {env_name}`.",
            file=sys.stderr,
        )
        return 2
    try:
        render = render_env(config, env_name)
    except RenderError as exc:
        print(f"dk {env_name}: {exc}", file=sys.stderr)
        return 1
    if _check(config, env_name, render, strict=False) != 0:
        print(f"dk {env_name}: not touching the host until the render is committed.", file=sys.stderr)
        return 1

    c = h.load_committed(generated_dir(config, env_name))
    if target is None:
        from catalpa_tooling.ssh_known_hosts import ensure_known_host

        target = h.host_from_docker_host(info.get("docker_host"), None)
        if ensure_known_host(target.address, port=target.port, recovery_env_name=env_name) != 0:
            return 1

    try:
        plan = h.make_plan(c, h.read_host(target, c))
        if not plan.secrets_in_sync:
            for line in h.describe(plan, show_diff=False):
                print(line)
            sys.stdout.flush()
            print(
                f"dk {env_name}: secrets are missing on the host; push them first "
                f"(`dk {env_name} secrets push`).",
                file=sys.stderr,
            )
            return 1

        restarted: list[str] = []
        if plan.files_in_sync:
            print(f"{target.address}: files match the render.", file=sys.stderr)
        else:
            rc, restarted = apply_plan(
                c, target, plan, h.describe(plan), env_name,
                dry_run=dry_run, yes=yes, force=False, no_restart=False, more_follows=True,
            )  # fmt: skip
            if rc != 0:
                return rc

        images = sorted(account_images(c))
        if dry_run:
            print(f"dry-run: would pull {', '.join(images)}", file=sys.stderr)
            print("dry-run: and restart the units whose image changed.", file=sys.stderr)
            print("dry-run: nothing changed.", file=sys.stderr)
            return 0
        print(f"{target.address}: pulling {len(images)} images …", file=sys.stderr)
        out = target.ssh(c.user, "sh -s", input=pull_script(c, images).encode()).stdout.decode()
        changed = [line[len(CHANGED):] for line in out.splitlines() if line.startswith(CHANGED)]
        units = [u for u in units_for_images(c, changed) if u not in restarted]
        if changed:
            print(f"New images: {', '.join(changed)}", file=sys.stderr)
        if units:
            print(f"{target.address}: systemctl --user restart {' '.join(units)}", file=sys.stderr)
            target.ssh(c.user, "systemctl --user restart " + " ".join(shlex.quote(u) for u in units))
        elif not changed:
            print(f"{target.address}: every image is current.", file=sys.stderr)
        start_what_is_down(target, c)
    except h.HostError as exc:
        print(f"dk {env_name}: {exc}", file=sys.stderr)
        return 1

    user_units, _system = h.stack_units(c)
    r = target.ssh(c.user, "systemctl --user is-active " + " ".join(user_units), check=False)
    states = r.stdout.decode().split()
    for unit, state in zip(user_units, states, strict=False):
        print(f"  {unit:28} {state}", file=sys.stderr)
    if any(s != "active" for s in states):
        print(f"dk {env_name}: not every unit is active (`dk {env_name} logs <service>`).", file=sys.stderr)
        return 1
    print(f"dk {env_name}: up to date.", file=sys.stderr)
    return 0
