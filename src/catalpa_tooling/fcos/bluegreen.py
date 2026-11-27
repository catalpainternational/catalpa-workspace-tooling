"""A blue/green site on a shared server: one account for the site, one per colour.

The project ships its own units (a folder with ``app.toml``, ``units/`` and ``bin/``, as
Partisipa's ``infra/quadlets``), and ``info.yaml`` says which version each account runs:

.. code-block:: yaml

    runtime: quadlet
    host: ../catalpa-hosts/shared-1     # the server's folder (its host.yaml)
    docker_host: ssh://site@shared-1.example.org/run/user/1002/podman/podman.sock
    bluegreen:
      units: infra/quadlets             # the folder with app.toml, in this repo
      site: {ref: v3.8}                 # the database, the front and the hooks' scripts
      blue: {ref: v3.7}
      green: {ref: v3.8}                # a ref, or `path:` inside `units` (default: the tree)

``app.toml`` says which units belong to the site and which to a colour, the ports, and the
hooks (``[site]``, ``[colour]``, ``[ports]``, ``[hooks]``: docs/MULTI_ENV_HOST.md). Each account
installs only its own version, so blue and green can run different versions of units with the
same names. dk writes one drop-in per unit that publishes a port: the port on the host's
loopback (the account's slot decides it), and the network that reaches the other accounts'.

``dk <env> render`` writes ``generated/fcos/{site,blue,green}/``; ``dk <env> secrets push``
creates each account's secrets from credentials.yaml (``site:``, ``blue:``, ``green:``);
``dk <env> deploy [blue|green]`` moves the site to the other colour, ``dk <env> rollback`` back,
and ``dk <env> colours`` says which is live. dk drives each step over SSH as each account.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from catalpa_tooling.fcos.shared_host import COLOURS, HOST_LOOPBACK, Account, HostConfig

ROLES = ("site", *COLOURS)
#: Unit files Quadlet reads; anything else (a .target, .timer, .service) is a plain systemd unit.
QUADLET_TYPES = (".container", ".pod", ".network", ".volume", ".kube", ".image", ".build")
#: How long a colour's units get to start (its migration runs then), and its health check.
START_SECONDS = 600
HEALTH_SECONDS = 60
DK_DROPIN = "50-dk.conf"
#: A colour's unit that starts it at boot once the site's ports answer: the site is another
#: account, so systemd can't order the colour after it.
BOOT_UNIT = "dk-colour-boot.service"


class BlueGreenError(RuntimeError):
    """An invalid ``bluegreen:``/``app.toml``, or a deploy that can't go on."""


# --- info.yaml and app.toml -------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One account's version: a git ref of this repo, and/or a folder inside ``units``."""

    ref: str | None = None
    path: str = "."

    def label(self) -> str:
        where = "" if self.path == "." else self.path
        if self.ref:
            return f"{self.ref}{':' + where if where else ''}"
        return where or "(working tree)"


@dataclass(frozen=True)
class BlueGreen:
    units: str
    sources: dict[str, Source]


def parse_bluegreen(info: dict) -> BlueGreen:
    raw = info.get("bluegreen")
    if not isinstance(raw, dict) or set(raw) - {"units", *ROLES}:
        raise BlueGreenError("bluegreen takes units, site, blue and green")
    units = str(raw.get("units") or "").strip().rstrip("/")
    if not units or units.startswith("/") or ".." in Path(units).parts:
        raise BlueGreenError("bluegreen.units: the folder (relative to this repo) with the app's app.toml")
    sources = {}
    for role in ROLES:
        r = raw.get(role) or {}
        if not isinstance(r, dict) or set(r) - {"ref", "path"}:
            raise BlueGreenError(f"bluegreen.{role} takes ref and path")
        path = str(r.get("path") or ".").strip("/") or "."
        if ".." in Path(path).parts:
            raise BlueGreenError(f"bluegreen.{role}.path stays inside bluegreen.units")
        sources[role] = Source(ref=str(r["ref"]) if r.get("ref") else None, path=path)
    return BlueGreen(units=units, sources=sources)


@dataclass(frozen=True)
class Port:
    role: str  # "site" or "colour"
    unit: str
    container_port: int


@dataclass(frozen=True)
class AppToml:
    set: str
    health: str
    units: dict[str, tuple[str, ...]]  # "site" / "colour" -> unit files
    start: dict[str, tuple[str, ...]]
    secrets: dict[str, tuple[str, ...]]
    ports: dict[str, Port]
    hooks: dict[str, str]


HOOKS = ("frozen", "freeze", "thaw", "done", "switch", "rollback", "keep")
#: What dk fills in a hook's command.
#: What dk fills in each hook's command: switch gets the colour and its port, rollback the
#: snapshot, every other hook only the env.
HOOK_FIELDS = {"switch": ("env", "colour", "port"), "rollback": ("env", "snapshot")}
#: A unit or script name: no folder, no hidden file. dk installs them as root, so a name that
#: climbed out of the account's folder would put a file anywhere.
_FILE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]*$")


def _file_name(name: str, where: str) -> str:
    if not _FILE_NAME.fullmatch(name):
        raise BlueGreenError(f"{where}: {name!r} isn't a plain file name")
    return name


def parse_app_toml(text: str, where: str) -> AppToml:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BlueGreenError(f"{where}: {exc}") from exc
    set_name = str(raw.get("set") or "")
    if not set_name:
        raise BlueGreenError(f"{where}: set (the unit set's name, as in its secrets) is missing")
    health = str(raw.get("health") or "/")
    units, start, secrets = {}, {}, {}
    for part in ("site", "colour"):
        sec = raw.get(part)
        if not isinstance(sec, dict) or not sec.get("units") or not sec.get("start"):
            raise BlueGreenError(f"{where}: [{part}] needs units and start")
        units[part] = tuple(_file_name(str(u), f"{where}: [{part}] units") for u in sec["units"])
        start[part] = tuple(str(u) for u in sec["start"])
        secrets[part] = tuple(str(s) for s in sec.get("secrets") or ())
    ports: dict[str, Port] = {}
    for name, spec in (raw.get("ports") or {}).items():
        bits = str(spec).split(":")
        if len(bits) != 3 or bits[0] not in ("site", "colour") or not bits[2].isdigit():
            raise BlueGreenError(f'{where}: ports.{name} = "<site|colour>:<unit file>:<container port>"')
        if bits[1] not in units[bits[0]]:
            raise BlueGreenError(f"{where}: ports.{name}: {bits[1]} isn't one of [{bits[0]}] units")
        ports[str(name)] = Port(bits[0], bits[1], int(bits[2]))
    for name, role in (("edge", "site"), ("colour", "colour")):
        if name not in ports or ports[name].role != role:
            raise BlueGreenError(
                f'{where}: ports.{name} = "{role}:…" is needed (edge: where the shared edge sends '
                "the site's names; colour: where the site's front sends a colour's visitors)"
            )
    extra = [n for n, p in ports.items() if p.role == "colour" and n != "colour"]
    if extra:
        raise BlueGreenError(f"{where}: a colour publishes one port (ports.colour), not {extra}")
    hooks = {str(k): str(v) for k, v in (raw.get("hooks") or {}).items()}
    unknown = sorted(set(hooks) - set(HOOKS))
    if unknown:
        raise BlueGreenError(f"{where}: unknown hooks {unknown}; dk runs {', '.join(HOOKS)}")
    for needed in ("freeze", "thaw", "switch"):
        if needed not in hooks:
            raise BlueGreenError(f"{where}: [hooks] needs {needed}")
    for name, template in hooks.items():
        fields = HOOK_FIELDS.get(name, ("env",))
        try:
            template.format(**{f: "x" for f in fields})
        except (KeyError, IndexError, ValueError, AttributeError, TypeError) as exc:
            raise BlueGreenError(
                f"{where}: hooks.{name}: dk fills only {', '.join('{' + f + '}' for f in fields)} "
                f"there; write a literal brace as {{{{ or }}}} ({exc!r})"
            ) from exc
    return AppToml(set_name, health, units, start, secrets, ports, hooks)


def site_ports(app: AppToml, site: Account) -> dict[str, int]:
    """Each site port's loopback port: ``edge`` the account's first, the rest in order."""
    out = {"edge": site.port(0)}
    n = 1
    for name, p in app.ports.items():
        if p.role == "site" and name != "edge":
            out[name] = site.port(n)
            n += 1
    return out


# --- reading a version ------------------------------------------------------------------------


Git = Callable[[list[str]], str]


def _git(repo_root: Path) -> Git:
    def run(args: list[str]) -> str:
        r = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise BlueGreenError(f"git {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout

    return run


@dataclass
class Version:
    """One account's files: from ``ref`` in git, or from the working tree."""

    repo_root: Path
    folder: str  # units/<path>, relative to the repo
    ref: str | None
    git: Git

    def read(self, rel: str) -> str:
        path = posixpath.normpath(posixpath.join(self.folder, rel))
        if self.ref:
            return self.git(["show", f"{self.ref}:{path}"])
        p = self.repo_root / path
        if not p.is_file():
            raise BlueGreenError(f"{p} is missing")
        return p.read_text(encoding="utf-8")

    def exists(self, rel: str) -> bool:
        try:
            self.read(rel)
        except BlueGreenError:
            return False
        return True

    def listdir(self, rel: str) -> list[str]:
        path = posixpath.normpath(posixpath.join(self.folder, rel))
        if self.ref:
            out = self.git(["ls-tree", f"{self.ref}:{path}"]) if self.exists_dir(rel) else ""
            # "<mode> <type> <object>\t<name>": files only, as the working tree lists them.
            return sorted(
                line.split("\t", 1)[1] for line in out.splitlines() if line.split()[1:2] == ["blob"]
            )
        p = self.repo_root / path
        return sorted(x.name for x in p.iterdir() if x.is_file()) if p.is_dir() else []

    def exists_dir(self, rel: str) -> bool:
        path = posixpath.normpath(posixpath.join(self.folder, rel))
        try:
            self.git(["rev-parse", "--verify", "--quiet", f"{self.ref}:{path}"])
        except BlueGreenError:
            return False
        return True


def version_of(repo_root: Path, bg: BlueGreen, role: str, git: Git | None = None) -> Version:
    src = bg.sources[role]
    folder = posixpath.normpath(posixpath.join(bg.units, src.path))
    return Version(repo_root, folder, src.ref, git or _git(repo_root))


# --- render -----------------------------------------------------------------------------------


def config_dir(project: str, env_name: str, role: str) -> str:
    return f"/etc/dk/{project}/{env_name}/{role}"


def instance(env_name: str, role: str) -> str:
    return env_name if role == "site" else f"{env_name}-{role}"


def secret_name(app: AppToml, env_name: str, role: str, secret: str) -> str:
    return f"{app.set}-{instance(env_name, role)}-{secret}"


def _dropin(unit: str, publish: list[tuple[int, int]]) -> str:
    section = "Pod" if unit.endswith(".pod") else "Container"
    lines = [
        "# Generated by dk: this account's ports on the host's loopback (its slot in host.yaml),",
        "# and a network that reaches the other accounts' ports there at " + HOST_LOOPBACK + ".",
        f"[{section}]",
        "Network=",
        f"Network=pasta:--map-host-loopback,{HOST_LOOPBACK}",
    ]
    lines += [f"PublishPort=127.0.0.1:{host_port}:{cport}" for host_port, cport in publish]
    return "\n".join(lines) + "\n"


def boot_unit(start: list[str], site_ports: list[int]) -> str:
    waits = " ".join(str(p) for p in site_ports)
    targets = " ".join(start)
    return (
        "# Generated by dk: at boot, start this colour once the site account's ports answer.\n"
        "[Unit]\n"
        "Description=dk: start this colour after the site (another account)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "TimeoutStartSec=15min\n"
        f"ExecStart=/bin/bash -c 'for p in {waits}; do until (exec 3<>/dev/tcp/127.0.0.1/$p) 2>/dev/null; do sleep 2; done; done'\n"
        f"ExecStart=/usr/bin/systemctl --user start --no-block {targets}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_bluegreen(
    project: str,
    env_name: str,
    bg: BlueGreen,
    host: HostConfig,
    repo_root: Path,
    *,
    git: Git | None = None,
) -> dict[str, str]:
    """``generated/fcos/{site,blue,green}/``: each account's units, dk's drop-ins and a
    manifest ``dk <env> deploy`` installs from (the same shape ``apply`` reads)."""
    accounts = host.accounts_of(project, env_name)
    if sorted(accounts) != sorted(ROLES):
        raise BlueGreenError(
            f"{host.dir / 'host.yaml'} needs site, blue and green accounts for {project} "
            f"{env_name} (it has {sorted(accounts) or 'none'}). Accounts are made only at a "
            "server's first boot: add them there and build a new server"
        )
    files: dict[str, str] = {}
    site_app = parse_app_toml(version_of(repo_root, bg, "site", git).read("app.toml"), f"{bg.units} (site)")
    site_port_list = sorted(site_ports(site_app, accounts["site"]).values())
    for role in ROLES:
        v = version_of(repo_root, bg, role, git)
        app = parse_app_toml(v.read("app.toml"), f"{v.folder}/app.toml ({bg.sources[role].label()})")
        part = "site" if role == "site" else "colour"
        a = accounts[role]
        cdir = config_dir(project, env_name, role)
        install: list[dict] = []
        unit_dir = f"{a.home}/.config/systemd/user"
        if part == "site":
            ports = site_ports(app, a)
        else:
            ports = {"colour": a.port(0)}
        publish: dict[str, list[tuple[int, int]]] = {}
        for name, host_port in ports.items():
            p = app.ports[name]
            publish.setdefault(p.unit, []).append((host_port, p.container_port))
        for unit in app.units[part]:
            text = v.read(f"units/{unit}")
            key = f"{role}/units/{unit}"
            files[key] = text
            quadlet = unit.endswith(QUADLET_TYPES)
            dest = f"{a.quadlet_dir}/{unit}" if quadlet else f"{unit_dir}/{unit}"
            install.append(
                {"file": key[len(role) + 1:], "dest": dest, "owner": "root" if quadlet else a.name, "mode": "0644"}
            )
            if unit in publish:
                if not quadlet:
                    raise BlueGreenError(f"ports: {unit} isn't a Quadlet unit, so it can't publish a port")
                dkey = f"{role}/units/{unit}.d/{DK_DROPIN}"
                files[dkey] = _dropin(unit, publish[unit])
                install.append(
                    {"file": dkey[len(role) + 1:], "dest": f"{a.quadlet_dir}/{unit}.d/{DK_DROPIN}",
                     "owner": "root", "mode": "0644"}
                )  # fmt: skip
        if part == "site":
            for name in v.listdir("bin"):
                _file_name(name, f"{v.folder}/bin")
                key = f"site/bin/{name}"
                files[key] = v.read(f"bin/{name}")
                install.append({"file": f"bin/{name}", "dest": f"{cdir}/bin/{name}", "owner": "root", "mode": "0755"})
        start = [u.format(env=env_name, colour=role) for u in app.start[part]]
        if part == "colour":
            files[f"{role}/units/{BOOT_UNIT}"] = boot_unit(start, site_port_list)
            install.append(
                {"file": f"units/{BOOT_UNIT}", "dest": f"{unit_dir}/{BOOT_UNIT}", "owner": a.name, "mode": "0644"}
            )
        manifest = {
            "host": host.name,
            "project": project,
            "env": env_name,
            "role": role,
            "user": a.name,
            "uid": a.uid,
            "config_dir": cdir,
            "source": {"ref": bg.sources[role].ref, "path": bg.sources[role].path},
            "units_version": v.read("units-version").strip() if v.exists("units-version") else None,
            "set": app.set,
            "start": start,
            "ports": ports,
            "secrets": {secret_name(app, env_name, role, s): s for s in app.secrets[part]},
            "health": app.health,
            "masked_units": [],
            "timers": [],
            "links": {},
            "bind_volumes": {},
        }
        if part == "site":
            manifest["hooks"] = app.hooks
        install.append({"file": "manifest.json", "dest": f"{cdir}/fcos-manifest.json", "owner": "root", "mode": "0644"})
        manifest["install"] = sorted(install, key=lambda e: e["file"])
        files[f"{role}/manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    return files


# --- the deploy -------------------------------------------------------------------------------


@dataclass
class Site:
    """The three accounts' committed renders and the SSH target."""

    env_name: str
    manifests: dict[str, dict]
    committed: dict  # role -> catalpa_tooling.fcos.host.Committed
    target: object  # catalpa_tooling.fcos.host.Host
    say: Callable[[str], None] = field(default=lambda m: print(m, file=sys.stderr))
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    def user(self, role: str) -> str:
        return self.manifests[role]["user"]

    @property
    def state_dir(self) -> str:
        return f".local/state/dk/{self.env_name}"

    def run(self, role: str, command: str, *, check: bool = True, input: bytes | None = None):
        return self.target.ssh(self.user(role), command, check=check, input=input)

    def out(self, role: str, command: str) -> str:
        return self.run(role, command).stdout.decode().strip()

    # site-account state: live, mark, rollback record, lock
    def read_state(self, name: str) -> str:
        r = self.run("site", f"cat {self.state_dir}/{name} 2>/dev/null", check=False)
        return r.stdout.decode().strip() if r.returncode == 0 else ""

    def write_state(self, name: str, value: str) -> None:
        self.run("site", f"mkdir -p {self.state_dir} && printf '%s\\n' {shlex.quote(value)} > {self.state_dir}/{name}")

    def clear_state(self, name: str) -> None:
        self.run("site", f"rm -f {self.state_dir}/{name}")

    def lock(self) -> bool:
        r = self.run("site", f"mkdir -p {self.state_dir} && mkdir {self.state_dir}/lock", check=False)
        return r.returncode == 0

    def unlock(self) -> None:
        self.run("site", f"rmdir {self.state_dir}/lock", check=False)

    def hook(self, name: str, **values: object) -> subprocess.CompletedProcess:
        template = self.manifests["site"]["hooks"].get(name)
        if template is None:
            return subprocess.CompletedProcess([], 0, b"", b"")
        values = {"env": self.env_name, **values}
        cmd = template.format(**{k: shlex.quote(str(v)) for k, v in values.items()})
        self.say(f"  site: {cmd}")
        cdir = self.manifests["site"]["config_dir"]
        return self.run("site", f"cd {shlex.quote(cdir)} && {cmd}", check=False)

    def start(self, role: str) -> None:
        units = " ".join(shlex.quote(u) for u in self.manifests[role]["start"])
        self.run(role, f"systemctl --user reset-failed; systemctl --user start --no-block {units}")

    def stop(self, role: str) -> None:
        """Stop the start targets and every unit they want, and wait for all of them: stopping
        only a target returns before its units have (a web taking 10 s to stop would still be
        answering, and writing, when the database is frozen)."""
        units = " ".join(shlex.quote(u) for u in self.manifests[role]["start"])
        self.run(role, STOP_SCRIPT.replace("@UNITS@", units), check=False)

    def wait_started(self, role: str) -> str | None:
        """None once every unit the start targets want is up (a oneshot done, a skipped
        condition fine); else what failed. Fails as soon as one unit fails."""
        deadline = self.clock() + START_SECONDS
        script = WAIT_SCRIPT.replace("@UNITS@", " ".join(shlex.quote(u) for u in self.manifests[role]["start"]))
        while True:
            lines = self.out(role, script).splitlines()
            failed = [line.split()[0] for line in lines if line.endswith(" failed")]
            waiting = [line.split()[0] for line in lines if line.endswith(" waiting")]
            if failed:
                return "failed: " + ", ".join(failed)
            if not waiting:
                return None
            if self.clock() > deadline:
                return f"still starting after {START_SECONDS} s: " + ", ".join(waiting)
            self.sleep(2)

    def healthy(self, role: str) -> bool:
        port = self.manifests[role]["ports"]["colour"]
        url = f"http://127.0.0.1:{port}{self.manifests[role]['health']}"
        deadline = self.clock() + HEALTH_SECONDS
        while True:
            r = self.run("site", f"curl -fsS -m 5 -o /dev/null {shlex.quote(url)}", check=False)
            if r.returncode == 0:
                return True
            if self.clock() > deadline:
                return False
            self.sleep(1)

    def enable_at_boot(self, live: str | None) -> None:
        """The site and only the live colour start at boot; the other colour would share its
        database. None: no colour, while a deploy has the database set aside (a reboot then
        must not start the old colour on the copy being migrated). A colour starts through
        dk's boot unit, which waits for the site's ports."""
        wants = ".config/systemd/user/default.target.wants"
        for unit in self.manifests["site"]["start"]:
            self.run("site", f"mkdir -p {wants} && ln -sfn ../{shlex.quote(_template_of(unit))} {wants}/{shlex.quote(unit)}")
        for role in COLOURS:
            link = f"{wants}/{BOOT_UNIT}"
            # A colour's own targets are started by the boot unit, never linked directly.
            targets = " ".join(f"{wants}/{shlex.quote(u)}" for u in self.manifests[role]["start"])
            self.run(role, f"rm -f {targets}")
            if role == live:
                self.run(role, f"mkdir -p {wants} && ln -sfn ../{BOOT_UNIT} {link}")
            else:
                self.run(role, f"rm -f {link}")


#: Stop the targets and what they want, in one systemctl call, which waits for every stop job.
STOP_SCRIPT = r"""units=""
for t in @UNITS@; do
  units="$units $t $(systemctl --user list-dependencies --plain --no-legend "$t" | sed 's/^[^a-zA-Z0-9]*//' | tr '\n' ' ')"
done
systemctl --user stop $units"""


#: Each unit the start targets want, as "<unit> active|waiting|failed". A oneshot that finished
#: (its Result success, even without RemainAfterExit) and a skipped condition count as active.
WAIT_SCRIPT = r"""for t in @UNITS@; do
  for u in "$t" $(systemctl --user list-dependencies --plain --no-legend "$t" | sed 's/^[^a-zA-Z0-9]*//'); do
    a=$(systemctl --user show -p ActiveState --value "$u")
    r=$(systemctl --user show -p Result --value "$u")
    c=$(systemctl --user show -p ConditionResult --value "$u")
    x=$(systemctl --user show -p ExecMainExitTimestampMonotonic --value "$u")
    st=$(systemctl --user show -p SubState --value "$u")
    if [ "$a" = failed ] || [ "$st" = auto-restart ]; then s=failed
    elif [ "$a" = active ]; then s=active
    elif [ "$c" = no ]; then s=active
    elif [ "$a" = inactive ] && [ "$r" = success ] && [ -n "$x" ] && [ "$x" != 0 ]; then s=active
    elif [ "$a" = inactive ] && [ -n "$r" ] && [ "$r" != success ]; then s=failed
    else s=waiting
    fi
    echo "$u $s"
  done
done"""


def _template_of(unit: str) -> str:
    """``notes@staging-blue.target`` → ``notes@.target``."""
    if "@" not in unit:
        return unit
    head, rest = unit.split("@", 1)
    return f"{head}@{rest[rest.rfind('.'):]}"


def other(colour: str) -> str:
    return "green" if colour == "blue" else "blue"


def install(site: Site, role: str) -> bool:
    """Put ``role``'s committed files on its account (as root), reload its systemd. True when
    anything changed."""
    from catalpa_tooling.fcos import host as h

    c = site.committed[role]
    plan = h.make_plan(c, h.read_host(site.target, c))
    if plan.files_in_sync:
        return False
    site.target.root_script(h.apply_script(c, plan))
    site.run(role, "systemctl --user daemon-reload")
    site.say(f"  {role} ({site.user(role)}): {len(plan.changes)} files installed, {len(plan.remove)} removed")
    return True


def missing_secrets(site: Site, roles: tuple[str, ...]) -> list[str]:
    out = []
    for role in roles:
        have = set(site.out(role, "podman secret ls --format '{{.Name}}'").split())
        out += [f"{site.user(role)}: {n}" for n in site.manifests[role]["secrets"] if n not in have]
    return out


def pull(site: Site, role: str) -> None:
    images = set()
    for entry in site.manifests[role]["install"]:
        text = site.committed[role].files.get(entry["file"], "")
        for line in text.splitlines():
            if line.startswith("Image=") and not line[6:].startswith("localhost/"):
                images.add(line[6:].strip())
    for image in sorted(images):
        site.say(f"  {role}: pulling {image}")
        site.run(role, f"podman pull -q {shlex.quote(image)} >/dev/null")


def deploy(site: Site, colour: str | None = None) -> int:
    """Move the site to ``colour`` (default: the one not live). Partisipa's order
    (deploy-site / containerwharf's sites deploy): a failure before the live colour stops
    changes nothing a visitor sees, and one after it puts the old colour and database back.
    Not covered: when the site's own files changed, its units restart before that, outside the
    put-back."""
    say = site.say
    if not site.lock():
        say("Another deploy holds the lock (~/.local/state/dk/<env>/lock in the site account).")
        return 1
    try:
        mark = site.read_state("mark")
        if mark:
            say(f"Refused: a deploy or rollback didn't finish ({mark}). Run `dk {site.env_name} rollback`.")
            return 1
        live = site.read_state("live") or None
        idle = colour or (other(live) if live else "blue")
        if idle == live:
            say(f"{idle} is already live.")
            return 1
        say(f"Deploying {_label(site.manifests[idle])} to {idle} ({site.user(idle)}); live: {live or 'none'}.")

        missing = missing_secrets(site, ("site", idle))
        if missing:
            say("Secrets are missing: " + ", ".join(missing) + f". `dk {site.env_name} secrets push` first.")
            return 1
        if install(site, "site"):
            site.run("site", "systemctl --user restart " + " ".join(site.manifests["site"]["start"]))
        site.start("site")
        problem = site.wait_started("site")
        if problem:
            say(f"The site's own units didn't start ({problem}).")
            _journal(site, "site")
            return 1
        install(site, idle)
        # The idle colour now holds the new version: it is no longer something to go back to.
        site.clear_state("rollback")
        pull(site, idle)

        if live:
            r = site.hook("frozen")
            if r.returncode == 0:
                say("  a database set aside by an earlier deploy: keeping it as a snapshot")
                site.hook("done")
            elif r.returncode != 1:
                say("Refused before stopping anything: the freeze hook can't run here "
                    f"(frozen exited {r.returncode}). Nothing a visitor sees has changed.")
                return 4
            # The record first: a mark without one would refuse every deploy and every rollback.
            site.write_state("rollback", live)
            site.write_state("mark", f"deploy {idle} from {live}, {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")

        started = site.clock()
        froze = False
        if live:
            say(f"Stopping {live}: the site is down until {idle} answers.")
            site.enable_at_boot(None)
            site.stop(live)
            froze = True  # before the hook: a freeze that fails part-way is thawed too
            if site.hook("freeze").returncode != 0:
                say("The freeze failed.")
                return put_back(site, idle, live, froze=froze)
        site.start(idle)
        problem = site.wait_started(idle)
        if problem:
            say(f"{idle} didn't start ({problem}).")
            _journal(site, idle)
            return put_back(site, idle, live, froze=froze)
        if not site.healthy(idle):
            say(f"{idle} doesn't answer on its port.")
            return put_back(site, idle, live, froze=froze)
        if site.hook("switch", colour=idle, port=site.manifests[idle]["ports"]["colour"]).returncode != 0:
            say("The switch failed.")
            return put_back(site, idle, live, froze=froze)
        site.write_state("live", idle)
        site.enable_at_boot(idle)
        if live:
            say(f"{idle} is live; the site was down {site.clock() - started:.0f} s.")
            r = site.hook("done")
            snapshot = r.stdout.decode().strip().splitlines()[-1] if r.stdout.strip() else ""
            site.write_state("rollback", f"{live} {snapshot}".strip())
            site.clear_state("mark")
        else:
            say(f"{idle} is live (the first deploy: nothing was stopped).")
        return 0
    finally:
        site.unlock()


def put_back(site: Site, idle: str, live: str | None, *, froze: bool) -> int:
    say = site.say
    site.stop(idle)
    if not live:
        say(f"Stopped {idle}; nothing was live, so nothing to put back.")
        return 1
    ok = True
    if froze and site.hook("thaw").returncode != 0:
        say("Putting the database back failed.")
        ok = False
    if ok:
        site.start(live)
        ok = site.wait_started(live) is None and site.healthy(live)
        ok = ok and site.hook("switch", colour=live, port=site.manifests[live]["ports"]["colour"]).returncode == 0
    if ok:
        site.enable_at_boot(live)
        site.clear_state("rollback")
        site.clear_state("mark")
        say(f"Put back: {live} is live again, with the database as it was.")
    else:
        say(f"Couldn't put {live} back; the mark stays and deploys are refused until "
            f"`dk {site.env_name} rollback`.")
    return 1


def rollback(site: Site) -> int:
    """Back one step: to the colour in the rollback record, with the database from before the
    last deploy. Also what recovers a deploy that couldn't put itself back (its mark names the
    colour it was deploying): as Partisipa's rollback-site, it restores the snapshot when there
    is one, else thaws what is still set aside, else leaves the database as it is."""
    say = site.say
    if not site.lock():
        say("Another deploy holds the lock.")
        return 1
    try:
        record = site.read_state("rollback").split()
        mark = site.read_state("mark")
        live = site.read_state("live") or None
        if not record:
            say("Nothing to roll back to (no deploy has finished since the last rollback).")
            return 1
        previous, snapshot = record[0], (record[1] if len(record) > 1 else "")
        # A deploy's mark ("deploy <colour> from <live>") names the colour that may be running.
        current = mark.split()[1] if mark.startswith("deploy ") else live
        # As Partisipa's rollback-site: first keep what is about to be thrown away (a dump that
        # is never pruned), and refuse before stopping anything if that fails.
        kept = site.hook("keep")
        if kept.returncode != 0:
            say("Keeping the current database failed; nothing changed.")
            return 1
        kept_path = kept.stdout.decode().strip().splitlines()[-1] if kept.stdout.strip() else ""
        if kept_path:
            say(f"  kept: {kept_path}")
        site.write_state("mark", f"rollback to {previous}, {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
        if kept_path:
            kept_note = f" (kept in {kept_path})."
        elif "keep" in site.manifests["site"]["hooks"]:
            kept_note = " (the keep hook kept them)."
        else:
            kept_note = ", and app.toml has no keep hook to keep them."
        say(f"Rolling back to {previous}; writes since the last deploy leave the site{kept_note}")
        site.enable_at_boot(None)
        if current:
            site.stop(current)
        if mark.startswith("deploy ") and previous != current:
            site.stop(previous)  # a failed put-back may have left it part-started
        if snapshot:
            ok = site.hook("rollback", snapshot=snapshot).returncode == 0
        else:
            frozen = site.hook("frozen").returncode
            ok = frozen == 1 or (frozen == 0 and site.hook("thaw").returncode == 0)
        if not ok:
            say("Putting the database back failed; the mark stays.")
            return 1
        site.start(previous)
        if site.wait_started(previous) or not site.healthy(previous):
            say(f"{previous} didn't come up; the mark stays.")
            return 1
        if site.hook("switch", colour=previous, port=site.manifests[previous]["ports"]["colour"]).returncode != 0:
            say("The switch failed; the mark stays.")
            return 1
        site.write_state("live", previous)
        site.enable_at_boot(previous)
        site.clear_state("rollback")
        site.clear_state("mark")
        say(f"{previous} is live again.")
        return 0
    finally:
        site.unlock()


def colours(site: Site) -> int:
    live = site.read_state("live") or "none"
    print(f"live: {live}")
    for name in ("mark", "rollback"):
        value = site.read_state(name)
        if value:
            print(f"{name}: {value}")
    for role in ROLES:
        m = site.manifests[role]
        states = site.run(role, "systemctl --user is-active " + " ".join(m["start"]), check=False)
        print(
            f"{role:5} {m['user']:8} {json.dumps(m['source'])} units-version {m['units_version']} "
            f"ports {m['ports']} {states.stdout.decode().split()}"
        )
    return 0


def _label(manifest: dict) -> str:
    src = manifest["source"]
    return Source(ref=src.get("ref"), path=src.get("path") or ".").label()


def _journal(site: Site, role: str) -> None:
    r = site.run(role, "journalctl --user -n 8 --no-pager -o cat -p warning", check=False)
    for line in r.stdout.decode().splitlines():
        site.say(f"    {line}")
