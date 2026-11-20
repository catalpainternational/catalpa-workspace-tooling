"""dk's everyday stack commands for an env with ``runtime: quadlet``.

Compose doesn't run these stacks, but each container still has its compose service's name
(``ContainerName=django``) and its own systemd unit (``django.service``) in the account's
systemd. So the commands map straight onto the host, over SSH as the account in
``docker_host``:

=========================  ===============================================================
``dk <env> logs [svc…]``   ``journalctl --user -u <svc>.service`` (container output and the
                           unit's own start/stop/failure messages, in one stream)
``dk <env> ps``            the stack units' state, then ``podman ps -a``
``dk <env> exec svc cmd``  ``podman exec``
``dk <env> manage …``      ``podman exec <web> ./manage.py …`` as ``stack.manage_user``
``dk <env> restart/stop/   ``systemctl --user`` on the services' units (all of the stack
start [svc…]``             when none is named, the edge's socket included)
=========================  ===============================================================

They read only ``docker_host`` and the committed ``generated/fcos/manifest.json``: no compose
file, no SOPS. Commands that would build or start a stack beside the quadlets (``up``,
``down``, ``run``, ``pull`` …) stay refused; ``dk <env> fcos apply`` is how a change reaches
the host.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from dataclasses import dataclass

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.fcos.host import Committed, Host, HostError, load_committed, run, stack_units
from catalpa_tooling.fcos.render import generated_dir

UNIT_VERBS = ("restart", "stop", "start")
SUPPORTED = ("logs", "ps", "exec", "manage", *UNIT_VERBS)


class Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse would exit
        raise Usage(f"{self.prog}: {message}")


@dataclass
class Backend:
    config: ProjectConfig
    env_name: str
    committed: Committed
    host: Host
    login: str

    @property
    def services(self) -> list[str]:
        """The compose services the render made containers for."""
        return sorted(self.committed.manifest.get("images") or {})

    def _check_services(self, names: list[str]) -> list[str]:
        unknown = [n for n in names if n not in self.services]
        if unknown:
            raise Usage(
                f"not a service of {self.env_name}: {', '.join(unknown)} "
                f"(services: {', '.join(self.services)})"
            )
        return names

    def _ssh(self, command: str, *, tty: bool) -> int:
        rc = self.host.stream(self.login, command, tty=tty)
        if rc == 255:
            print(
                f"ssh {self.login}@{self.host.address} failed. If the droplet was rebuilt, its host "
                f"key changed: check it's yours, then `ssh-keygen -R {self.host.address}`.",
                file=sys.stderr,
            )
        return rc

    def _proxy_socket(self) -> list[str]:
        proxy = self.committed.manifest.get("proxy")
        return [f"{proxy}.socket"] if proxy else []

    # --- verbs ------------------------------------------------------------------------------

    def logs(self, argv: list[str], *, tty: bool) -> int:
        p = _Parser(prog=f"dk {self.env_name} logs", add_help=False)
        p.add_argument("-f", "--follow", action="store_true")
        p.add_argument("-n", "--tail", default=None)
        p.add_argument("--since", default=None)
        p.add_argument("--until", default=None)
        p.add_argument("-t", "--timestamps", action="store_true")  # journalctl always has them
        p.add_argument("--no-color", action="store_true")
        p.add_argument("--no-log-prefix", action="store_true")
        p.add_argument("services", nargs="*")
        ns = p.parse_args(argv)
        services = self._check_services(ns.services) or self.services
        cmd = ["journalctl", "--user", "--no-pager"]
        for s in services:
            cmd += ["-u", f"{s}.service"]
        if ns.tail not in (None, "all"):
            cmd += ["-n", str(ns.tail)]
        if ns.since:
            cmd += ["--since", ns.since]
        if ns.until:
            cmd += ["--until", ns.until]
        if ns.follow:
            cmd.append("-f")
        # A tty keeps Ctrl-C going to the remote journalctl when following.
        return self._ssh(shlex.join(cmd), tty=tty and ns.follow)

    def ps(self, argv: list[str], *, tty: bool) -> int:
        if argv and argv != ["-a"] and argv != ["--all"]:
            raise Usage(f"dk {self.env_name} ps takes no arguments on a quadlet env")
        units = [f"{s}.service" for s in self.services] + self._proxy_socket()
        cmd = (
            "systemctl --user --no-pager --legend=false list-units --all "
            + " ".join(shlex.quote(u) for u in units)
            + "; echo; podman ps -a --format "
            + shlex.quote("table {{.Names}}\t{{.Status}}\t{{.Image}}")
        )
        return self._ssh(cmd, tty=False)

    def exec(self, argv: list[str], *, tty: bool) -> int:
        p = _Parser(prog=f"dk {self.env_name} exec", add_help=False)
        p.add_argument("-T", "--no-TTY", dest="no_tty", action="store_true")
        p.add_argument("-d", "--detach", action="store_true")
        p.add_argument("-u", "--user", default=None)
        p.add_argument("-e", "--env", action="append", default=[])
        p.add_argument("-w", "--workdir", default=None)
        p.add_argument("--privileged", action="store_true")
        p.add_argument("service")
        p.add_argument("command", nargs=argparse.REMAINDER)
        ns = p.parse_args(argv)
        if ns.privileged:
            raise Usage("--privileged: not on a quadlet env (the containers run unprivileged)")
        self._check_services([ns.service])
        if not ns.command:
            raise Usage(f"usage: dk {self.env_name} exec [options] SERVICE COMMAND [ARGS…]")
        use_tty = tty and not ns.no_tty and not ns.detach
        return self._exec(
            ns.service, ns.command, user=ns.user, env=ns.env, workdir=ns.workdir,
            tty=use_tty, detach=ns.detach,
        )  # fmt: skip

    def _exec(
        self,
        service: str,
        command: list[str],
        *,
        user: str | None = None,
        env: list[str] = (),
        workdir: str | None = None,
        tty: bool,
        detach: bool = False,
    ) -> int:
        cmd = ["podman", "exec"]
        cmd.append("-d" if detach else "-i")
        if tty:
            cmd.append("-t")
        if user:
            cmd += ["-u", user]
        for e in env:
            cmd += ["-e", e]
        if workdir:
            cmd += ["-w", workdir]
        cmd += [service, *command]
        return self._ssh(shlex.join(cmd), tty=tty)

    def manage(self, manage_args: list[str], *, user: str | None, tty: bool) -> int:
        if not manage_args:
            raise Usage(f"usage: dk {self.env_name} manage <manage.py arguments>")
        web = self.config.stack_service("web")
        self._check_services([web])
        return self._exec(
            web,
            ["./manage.py", *manage_args],
            user=user or self.config.stack.manage_user,
            tty=tty,
        )

    def units(self, verb: str, argv: list[str]) -> int:
        names = [a for a in argv if not a.startswith("-")]
        flags = [a for a in argv if a.startswith("-")]
        if flags:
            raise Usage(f"dk {self.env_name} {verb}: options aren't supported on a quadlet env ({' '.join(flags)})")
        self._check_services(names)
        if names:
            units = [f"{n}.service" for n in names]
        else:
            # The whole stack. The edge is socket-activated: stopping only its service would
            # let the next request start it again.
            # Restart leaves the socket alone: it hands the same listeners to the new process.
            user_units, _system = stack_units(self.committed)
            units = {
                "stop": self._proxy_socket() + user_units,
                "start": user_units + self._proxy_socket(),
                "restart": user_units,
            }[verb]
        cmd = f"systemctl --user {verb} " + " ".join(shlex.quote(u) for u in units)
        print(f"{self.host.address}: systemctl --user {verb} {' '.join(units)}", file=sys.stderr)
        return self._ssh(cmd, tty=False)


REFUSED_HINT = {
    "up": "`dk {env} fcos apply` installs a changed render and restarts what it changed",
    "down": "`dk {env} stop` stops the stack's units",
    "pull": "the units pull their images when they start; a new image_tag goes out with `fcos render` + `fcos apply`",
    "build": "the host pulls images; build and push them, then `fcos render` + `fcos apply`",
    "run": "`dk {env} exec SERVICE …` runs a command in a running container",
    "rm": "the units remove their containers when they stop",
    "wipe": "deleting a quadlet env's data is a host-level operation, not a dk command",
}


def handle(
    ns: argparse.Namespace,
    config: ProjectConfig,
    env_name: str,
    info: dict,
    *,
    runner=run,
    tty: bool | None = None,
) -> int:
    """Run a stack command for a quadlet env, or refuse it (2)."""
    command = getattr(ns, "env_command", None)
    if command == "manage":
        verb, argv = "manage", []
    elif command in (None, "compose"):
        argv = list(getattr(ns, "implicit_compose_argv", None) or getattr(ns, "compose_argv", None) or [])
        if not argv:
            argv = ["up", "-d"]
        verb, argv = argv[0], argv[1:]
    else:
        verb, argv = command, []

    if verb not in SUPPORTED:
        shown = " ".join([verb, *argv]) if command in (None, "compose") else command
        hint = REFUSED_HINT.get(verb, "").format(env=env_name)
        print(
            f"{env_name} runs as Podman quadlets (info.yaml `runtime: quadlet`), not compose, so "
            f"`dk {env_name} {shown}` isn't available. {hint + '. ' if hint else ''}"
            f"Available: {', '.join(SUPPORTED)}, `fcos …`, and `docker …` against the host's "
            "Podman socket.",
            file=sys.stderr,
        )
        return 2

    from catalpa_tooling.fcos.host import host_from_docker_host
    from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

    docker_host = str(info.get("docker_host") or "")
    try:
        committed = load_committed(generated_dir(config, env_name))
        host = host_from_docker_host(docker_host, None)
        login = parse_docker_host_to_ssh_target(docker_host).split("@", 1)[0]
    except (HostError, ValueError) as exc:
        print(f"dk {env_name} {verb}: {exc}", file=sys.stderr)
        return 1
    if login != committed.user:
        print(
            f"dk {env_name} {verb}: docker_host logs in as {login!r}, but the stack runs as "
            f"{committed.user!r} (fcos.user)",
            file=sys.stderr,
        )
        return 1
    dry_run = bool(getattr(ns, "dry_run", False))
    if runner is run and not dry_run:
        # As dk does for every deploy host: record its key on first contact (never replaces one).
        from catalpa_tooling.ssh_known_hosts import ensure_known_host

        if ensure_known_host(host.address, port=host.port, recovery_env_name=env_name) != 0:
            return 1
    host = Host(address=host.address, port=host.port, runner=_dry_runner if dry_run else runner)
    b = Backend(config=config, env_name=env_name, committed=committed, host=host, login=login)
    if tty is None:
        tty = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        if verb == "logs":
            return b.logs(argv, tty=tty)
        if verb == "ps":
            return b.ps(argv, tty=tty)
        if verb == "exec":
            return b.exec(argv, tty=tty)
        if verb == "manage":
            args = [a for a in getattr(ns, "manage_args", []) if a]
            return b.manage(args, user=getattr(ns, "user", None), tty=tty)
        return b.units(verb, argv)
    except Usage as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _dry_runner(argv, *, input=None, tty=False):
    import subprocess

    print("dry-run: " + shlex.join(argv), file=sys.stderr)
    return subprocess.CompletedProcess(argv, 0, b"", b"")


__all__ = ["Backend", "SUPPORTED", "handle", "run"]
