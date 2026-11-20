"""Rendered-host commands: ``dk <env> render|check|diff|apply``, ``dk <env> host
ignition|provision``, ``dk <env> secrets push`` (and the deprecated ``dk <env> fcos …``)."""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

import yaml

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.fcos.render import (
    Render,
    RenderError,
    compare_render,
    generated_dir,
    render_env,
    required_secret_problems,
    write_render,
)

HOST_COMMANDS = ("diff", "apply", "secrets", "provision")


#: ``dk <env> <verb>`` for a rendered host (tooling 2.0): the verbs ``dk <env> fcos …`` had.
RENDER_VERBS = ("render", "check", "diff", "apply")
#: ``dk <env> host <verb>``: a new host from the render.
HOST_VERBS = ("ignition", "provision")


def _flag(sp: argparse.ArgumentParser, *names: str, help: str) -> None:
    """A boolean flag that doesn't reset the env-level one: ``dk <env> --dry-run diff`` and
    ``dk <env> diff --dry-run`` both work (a subparser's default would overwrite the first)."""
    sp.add_argument(*names, action="store_true", default=argparse.SUPPRESS, help=help)


def _host_arg(sp: argparse.ArgumentParser) -> None:
    sp.add_argument(
        "--host",
        metavar="ADDRESS",
        help="SSH to this address instead of docker_host's (a new droplet before DNS moves).",
    )
    _flag(sp, "--dry-run", help="Show what would happen; change nothing.")


def add_render_verbs(sub: argparse._SubParsersAction) -> None:
    """render, check, diff, apply."""
    r = sub.add_parser(
        "render",
        help="Write docker/envs/<env>/generated/ from compose + info.yaml (commit the result).",
    )
    _flag(r, "--dry-run", help="List what would change; write nothing.")
    sub.add_parser(
        "check",
        help=(
            "Fail if the committed render differs from a fresh one, or if plain compose would "
            "start without a credential."
        ),
    )
    d = sub.add_parser(
        "diff",
        help="Compare the host's files, data directories, timers and secret names with the render.",
    )
    _host_arg(d)
    d.add_argument("--names-only", action="store_true", help="List what differs, without the diff.")
    a = sub.add_parser(
        "apply",
        help=(
            "Install what differs (as core, with sudo), reload systemd, restart only the units "
            "that read a changed file. A bare `dk <env>` does this and also pulls new images."
        ),
    )
    _host_arg(a)
    a.add_argument("--no-restart", action="store_true", help="Install and reload; restart nothing.")
    _flag(a, "--yes", "-y", help="Don't ask before changing the host.")
    a.add_argument(
        "--force",
        action="store_true",
        help="Change core's keys even when none of your public keys stays in fcos.admin_keys.",
    )


def add_host_verbs(sub: argparse._SubParsersAction) -> None:
    """ignition, provision (under ``host``)."""
    i = sub.add_parser(
        "ignition",
        help="Compile the rendered host.bu to Ignition (droplet user data) with Butane.",
    )
    i.add_argument("-o", "--output", metavar="FILE", help="Write here (default: stdout).")
    pv = sub.add_parser(
        "provision",
        help=(
            "After a droplet booted from the Ignition: check its files, push the secrets, log in "
            "to the registry, start the stack."
        ),
    )
    _host_arg(pv)
    pv.add_argument(
        "--no-start",
        action="store_true",
        help="Don't start the stack (restoring: the database must be restored first).",
    )


def add_secrets_push(sub: argparse._SubParsersAction) -> None:
    """push (under ``secrets``)."""
    sp = sub.add_parser(
        "push",
        help="Stream the render's secrets from credentials.yaml (sops) into Podman secrets on the host.",
    )
    _host_arg(sp)
    sp.add_argument("names", nargs="*", metavar="NAME", help="Only these secrets (default: all).")
    sp.add_argument(
        "--restart", action="store_true", help="Restart the units that use a pushed secret."
    )


def attach_fcos_parser(cmd_sub: argparse._SubParsersAction) -> None:
    """``dk <env> fcos …``: deprecated; the same commands as plain env verbs (``fcos_alias``)."""
    p = cmd_sub.add_parser(
        "fcos",
        help="Deprecated: use `dk <env> render|check|diff|apply`, `host ignition|provision`, `secrets push`.",
    )
    sub = p.add_subparsers(dest="fcos_command", required=True)
    add_render_verbs(sub)
    s = sub.add_parser("secrets", help="Podman secrets on the host.")
    add_secrets_push(s.add_subparsers(dest="fcos_secrets_command", required=True))
    add_host_verbs(sub)


def new_name(env_name: str, fcos_command: str) -> str:
    """The tooling 2.0 spelling of ``dk <env> fcos <fcos_command>``."""
    if fcos_command in HOST_VERBS:
        return f"dk {env_name} host {fcos_command}"
    if fcos_command == "secrets":
        return f"dk {env_name} secrets push"
    return f"dk {env_name} {fcos_command}"


def route(ns: argparse.Namespace, config: ProjectConfig, env_name: str, runtime: str) -> int | None:
    """Run a rendered-host command, or None when ``ns`` isn't one.

    ``dk <env> render|check|diff|apply``, ``host ignition|provision`` and ``secrets push``, and
    the deprecated ``dk <env> fcos …`` they replace."""
    cmd = getattr(ns, "env_command", None)
    if cmd in RENDER_VERBS:
        fcos_command = cmd
    elif cmd == "host" and getattr(ns, "host_command", None) in HOST_VERBS:
        fcos_command = ns.host_command
    elif cmd == "secrets" and getattr(ns, "secrets_command", None) == "push":
        fcos_command = "secrets"
        ns.fcos_secrets_command = "push"
    elif cmd == "fcos":
        print(
            f"note: `dk {env_name} fcos {ns.fcos_command}` is now "
            f"`{new_name(env_name, ns.fcos_command)}`; the `fcos` spelling will go.",
            file=sys.stderr,
        )
        return handle_fcos_command(ns, config, env_name)
    else:
        return None
    if runtime != "quadlet":
        print(
            f"dk {env_name}: `{new_name(env_name, fcos_command)}` is for envs whose host is rendered "
            f"(info.yaml `runtime: quadlet`); {env_name} runs compose. Compose hosts aren't "
            "rendered yet (tooling 2.0, docs/TOOLING_2.md M7).",
            file=sys.stderr,
        )
        return 2
    ns.fcos_command = fcos_command
    return handle_fcos_command(ns, config, env_name)


def _check(config: ProjectConfig, env_name: str, render: Render, *, strict: bool) -> int:
    gen_dir = generated_dir(config, env_name)
    rel = gen_dir.relative_to(config.repo_root)
    rc = 0
    diffs = compare_render(gen_dir, render.files)
    if diffs:
        print(
            f"{rel} differs from a fresh render (run `dk {env_name} render` and commit):",
            file=sys.stderr,
        )
        for line in diffs:
            print(f"  {line}", file=sys.stderr)
        rc = 1
    problems = required_secret_problems(config, render)
    if problems:
        print("credentials that plain `docker compose up` would run without:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        if strict:
            rc = 1
    if rc == 0 and strict:
        print(f"{rel}: matches a fresh render; every credential is required.", file=sys.stderr)
    return rc


def handle_fcos_command(ns: argparse.Namespace, config: ProjectConfig, env_name: str) -> int:
    command = ns.fcos_command
    gen_dir = generated_dir(config, env_name)
    rel = gen_dir.relative_to(config.repo_root)

    if command == "ignition":
        # Compiles what is committed; no fresh render needed (Butane reads only these files).
        return _ignition(ns, gen_dir)

    try:
        render = render_env(config, env_name)
    except RenderError as exc:
        print(f"{new_name(env_name, command)}: {exc}", file=sys.stderr)
        return 1

    if command == "render":
        if getattr(ns, "dry_run", False):
            for line in compare_render(gen_dir, render.files) or ["(no changes)"]:
                print(line)
            return 0
        changed = write_render(gen_dir, render.files)
        for line in changed:
            print(line)
        print(
            f"{rel}: {len(render.files)} files, {len(changed)} changed. Commit them; "
            f"`dk {env_name} check` compares them with a fresh render.",
            file=sys.stderr,
        )
        for p in required_secret_problems(config, render):
            print(f"warning: {p}", file=sys.stderr)
        return 0

    if command == "check":
        return _check(config, env_name, render, strict=True)

    # Host commands: only ever put on a host what is committed and matches a fresh render.
    if _check(config, env_name, render, strict=False) != 0:
        print(f"{new_name(env_name, command)}: not touching the host until the render is committed.", file=sys.stderr)
        return 1
    from catalpa_tooling.fcos.host import HostError

    try:
        return _host_command(ns, config, env_name, gen_dir)
    except HostError as exc:
        print(f"{new_name(env_name, command)}: {exc}", file=sys.stderr)
        return 1


def _ignition(ns: argparse.Namespace, gen_dir: Path) -> int:
    from catalpa_tooling.fcos.host import HostError, compile_ignition

    if not (gen_dir / "host.bu").is_file():
        print(f"{gen_dir / 'host.bu'} is missing: run `dk <env> render`", file=sys.stderr)
        return 1
    out = getattr(ns, "output", None)
    if not out and sys.stdout.isatty():
        print("host ignition: pass -o FILE, or redirect stdout", file=sys.stderr)
        return 1
    try:
        ign = compile_ignition(gen_dir)
    except HostError as exc:
        print(f"host ignition: {exc}", file=sys.stderr)
        return 1
    if out:
        Path(out).write_bytes(ign)
        print(f"{out}: {len(ign)} bytes (DigitalOcean user data: 64 KiB at most)", file=sys.stderr)
    else:
        sys.stdout.buffer.write(ign)
    return 0


def _host_command(
    ns: argparse.Namespace, config: ProjectConfig, env_name: str, gen_dir: Path
) -> int:
    from catalpa_tooling.fcos import host as h

    info_path = config.deploy_envs_dir / env_name / "info.yaml"
    info = yaml.safe_load(info_path.read_text(encoding="utf-8")) or {}
    c = h.load_committed(gen_dir)
    target = h.host_from_docker_host(info.get("docker_host"), getattr(ns, "host", None))
    dry_run = bool(getattr(ns, "dry_run", False))
    command = ns.fcos_command
    if not dry_run and command != "provision":  # provision waits for SSH and does this itself
        from catalpa_tooling.ssh_known_hosts import ensure_known_host

        if ensure_known_host(target.address, port=target.port, recovery_env_name=env_name) != 0:
            return 1

    if command == "secrets":
        return _secrets_push(ns, config, env_name, c, target, dry_run=dry_run)
    if command == "provision":
        return _provision(ns, config, env_name, c, target, dry_run=dry_run)

    state = h.read_host(target, c)
    plan = h.make_plan(c, state)
    lines = h.describe(plan, show_diff=not getattr(ns, "names_only", False))

    if command == "diff":
        for line in lines:
            print(line)
        sys.stdout.flush()
        if plan.files_in_sync and plan.secrets_in_sync:
            print(f"{target.address}: matches the render.", file=sys.stderr)
            return 0
        return 1

    # apply
    if plan.files_in_sync:
        for line in lines:
            print(line)
        sys.stdout.flush()
        print(f"{target.address}: files match the render; nothing to apply.", file=sys.stderr)
        return 0 if plan.secrets_in_sync else 1
    rc, _restarted = apply_plan(
        c, target, plan, lines, env_name,
        dry_run=dry_run, yes=bool(getattr(ns, "yes", False)),
        force=bool(getattr(ns, "force", False)), no_restart=bool(getattr(ns, "no_restart", False)),
    )  # fmt: skip
    if rc == 0 and not dry_run and not plan.secrets_in_sync:
        print(f"Secrets are missing: `dk {env_name} secrets push`.", file=sys.stderr)
    return rc


def apply_plan(
    c, target, plan, lines: list[str], env_name: str,
    *, dry_run: bool, yes: bool, force: bool, no_restart: bool, more_follows: bool = False,
) -> tuple[int, list[str]]:  # fmt: skip
    """Show ``plan``, ask, install it as root and restart what it affects.

    A change to comments only is installed but restarts nothing (a header reworded by a newer
    dk shouldn't restart the stack). ``more_follows``: the caller has more to do after a dry
    run, so don't end with "nothing changed".

    Returns (exit code, the account units it restarted). What ``dk <env> apply`` and a bare
    ``dk <env>`` share."""
    from catalpa_tooling.fcos import host as h

    restarts = h.restarts_for(c, [ch.dest for ch in plan.changes if ch.restarts] + plan.remove)
    for line in lines:
        print(line)
    # The plan goes to stdout, the rest to stderr: flush so a pipe keeps them in order.
    sys.stdout.flush()
    _print_restarts(restarts, no_restart=no_restart)
    if plan.changes and not any(ch.restarts for ch in plan.changes) and not plan.remove:
        print("Comments only: nothing to restart.", file=sys.stderr)
    lockout = h.lockout_problem(c, plan, h.local_public_keys())
    if lockout:
        print(f"{'warning' if force else 'refused'}: {lockout}", file=sys.stderr)
        if not force and not dry_run:
            print("Pass --force if that is what you want.", file=sys.stderr)
            return 1, []
    if dry_run:
        if not more_follows:
            print("dry-run: nothing changed.", file=sys.stderr)
        return 0, []
    if not yes:
        from catalpa_tooling.cli_confirm import confirm_yes_default_no

        if not confirm_yes_default_no(f"Apply this to {target.address} ({env_name})? [y/N]: "):
            print("Nothing changed.", file=sys.stderr)
            return 1, []
    root_after, user_after = h.restart_commands(c, plan, restarts, restart=not no_restart)
    target.root_script(h.apply_script(c, plan) + root_after + "\n")
    if user_after:
        target.ssh(c.user, "set -e\n" + user_after)
    print(
        f"{target.address}: {len(plan.changes)} files installed, {len(plan.remove)} removed.",
        file=sys.stderr,
    )
    restarted = [] if no_restart else [*restarts.user, *(f"{s}.service" for s in restarts.sockets)]
    return 0, restarted


def _print_restarts(r, *, no_restart: bool) -> None:
    units = (
        r.user
        + [f"{s}.socket" for s in r.sockets]
        + sorted(r.user_timers)
        + [f"{u} (system)" for u in r.system]
    )
    if units:
        verb = "Would restart (--no-restart: not restarted)" if no_restart else "Restarts"
        print(f"{verb}: {', '.join(units)}", file=sys.stderr)
    for note in r.notes:
        print(f"note: {note}", file=sys.stderr)


def _credentials(config: ProjectConfig, env_name: str) -> dict:
    from catalpa_tooling.sops_credentials import decrypt_credentials_yaml

    return decrypt_credentials_yaml(config.deploy_envs_dir / env_name / "credentials.yaml")


def _secrets_push(ns, config, env_name, c, target, *, dry_run: bool) -> int:
    from catalpa_tooling.fcos import host as h

    values, problems = h.secret_values(c, _credentials(config, env_name), ns.names or None)
    if problems:
        for p in problems:
            print(f"secrets push: {p}", file=sys.stderr)
        return 1
    if dry_run:
        for name, _value, system in values:
            print(f"dry-run: would push {'system ' if system else ''}secret {name}", file=sys.stderr)
        return 0
    h.push_secrets(target, c, values)
    r = h.restarts_for(c, [], secrets=[n for n, _v, _s in values])
    if ns.restart:
        if r.system:
            target.ssh("core", "sudo systemctl restart " + " ".join(r.system))
        if r.user:
            target.ssh(c.user, "systemctl --user restart " + " ".join(r.user))
        _print_restarts(r, no_restart=False)
    elif r.user or r.system:
        print(
            "Running containers keep the old values until restarted: "
            + ", ".join(r.user + [f"{u} (system)" for u in r.system])
            + " (or pass --restart)",
            file=sys.stderr,
        )
    return 0


def _provision(ns, config, env_name, c, target, *, dry_run: bool) -> int:
    from catalpa_tooling.fcos import host as h
    from catalpa_tooling.ssh_known_hosts import ensure_known_host

    if dry_run:
        user_units, system_units = h.stack_units(c)
        print(
            f"dry-run: on {target.address}: check the files match, push "
            f"{len(c.manifest.get('secrets') or {})} secrets and "
            f"{len(c.manifest.get('system_secrets') or {})} system secrets, log in to "
            f"{', '.join(h.private_registries(c)) or 'no registry'}"
            + ("" if ns.no_start else f", start {', '.join(user_units + system_units)}"),
            file=sys.stderr,
        )
        return 0
    if ensure_known_host(target.address, port=target.port, recovery_env_name=env_name) != 0:
        return 1

    # 1. Ignition placed the files: anything else means the droplet booted another render.
    plan = h.make_plan(c, h.read_host(target, c))
    if not plan.files_in_sync:
        for line in h.describe(plan, show_diff=False):
            print(line)
        print(
            f"{target.address} doesn't match the render (booted from an older Ignition?): "
            f"`dk {env_name} apply` first.",
            file=sys.stderr,
        )
        return 1
    for line in plan.dir_problems:
        print(f"! {line}", file=sys.stderr)

    # 2. Secrets.
    values, problems = h.secret_values(c, _credentials(config, env_name), None)
    if problems:
        for p in problems:
            print(f"host provision: {p}", file=sys.stderr)
        return 1
    print(f"Pushing {len(values)} secrets to {target.address}:", file=sys.stderr)
    h.push_secrets(target, c, values)

    # 3. Registry login, kept in the account's home (host.bu points REGISTRY_AUTH_FILE there).
    for registry in h.private_registries(c):
        if h.logged_in(target, c, registry):
            print(f"  {registry}: logged in", file=sys.stderr)
            continue
        cmd = f"podman login --authfile {shlex.quote(h.auth_file(c))} {shlex.quote(registry)}"
        if not sys.stdin.isatty():
            print(f"Log in to {registry}, then run provision again:\n  ssh -t {c.user}@{target.address} '{cmd}'", file=sys.stderr)
            return 1
        print(f"Log in to {registry} as {c.user} (the bot's token):", file=sys.stderr)
        if target.interactive(c.user, cmd) != 0:
            return 1

    if ns.no_start:
        print(
            "Not started (--no-start). Restore the database and media, then "
            f"`dk {env_name} host provision` again to start the stack.",
            file=sys.stderr,
        )
        return 0

    # 4. Start. The units tried at boot, before the secrets: clear their failures first.
    user_units, system_units = h.stack_units(c)
    if system_units:
        target.ssh(
            "core",
            "sudo systemctl reset-failed; sudo systemctl restart " + " ".join(system_units),
        )
    print(f"Starting {', '.join(user_units)} (first start pulls the images)…", file=sys.stderr)
    target.ssh(c.user, "systemctl --user reset-failed; systemctl --user start " + " ".join(user_units))
    r = target.ssh(
        c.user, "systemctl --user is-active " + " ".join(user_units), check=False
    )
    states = r.stdout.decode().split()
    for unit, state in zip(user_units, states, strict=False):
        print(f"  {unit}: {state}", file=sys.stderr)
    return 0 if r.returncode == 0 else 1
