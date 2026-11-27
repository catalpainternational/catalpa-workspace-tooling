"""Put a committed render on its FCOS host: ``dk <env> diff|apply``, ``secrets push``, ``host provision``.

Everything comes from the committed ``generated/fcos/`` (``manifest.json`` and the files its
install map names), and only after ``dk <env> check`` passes, so a host only ever runs what was
reviewed. Two logins, as on the host itself:

- ``core`` (passwordless sudo) reads and installs the files, root-owned or the account's, and
  manages the system units (the Zabbix agent and its secrets). One ``sudo sh`` per command,
  with the script on stdin.
- the account (``deploy``) owns the rootless Podman secrets and its own systemd: secrets push,
  ``systemctl --user``, the registry login.

A file is installed by writing a temporary file next to it and renaming it over the old one,
so a unit never reads half a file. Units restart only when something they read changed; a
one-shot started by a timer (restic) is never restarted.

Secrets are streamed from ``sops -d`` to ``podman secret create`` over SSH stdin: never
written to disk here, never on a command line, never printed.
"""

from __future__ import annotations

import base64
import difflib
import json
import posixpath
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from catalpa_tooling.fcos.butane import dir_mode, home_of, keys_dir
from catalpa_tooling.fcos.render import MANIFEST, host_manifest_path

#: Runs one command (argv), with optional stdin bytes; returns the finished process with
#: stdout/stderr as bytes. Tests replace it.
Runner = Callable[..., subprocess.CompletedProcess]


def run(argv: list[str], *, input: bytes | None = None, tty: bool = False) -> subprocess.CompletedProcess:
    """``tty``: inherit this process's stdin/stdout/stderr instead of capturing."""
    if tty:
        return subprocess.run(argv, check=False)
    return subprocess.run(argv, input=input, capture_output=True, check=False)


class HostError(RuntimeError):
    pass


@dataclass(frozen=True)
class Committed:
    """The committed render: the manifest and the bytes of every file it installs."""

    manifest: dict
    files: dict[str, str]

    @property
    def user(self) -> str:
        return self.manifest["user"]

    @property
    def uid(self) -> int:
        return int(self.manifest["uid"])

    @property
    def home(self) -> str:
        return f"/var/home/{self.user}"

    @property
    def quadlet_dir(self) -> str:
        return f"/etc/containers/systemd/users/{self.uid}"

    @property
    def user_unit_dir(self) -> str:
        return f"{self.home}/.config/systemd/user"

    @property
    def manifest_dest(self) -> str:
        return host_manifest_path(self.manifest["config_dir"])

    @property
    def key_dirs(self) -> list[str]:
        """``authorized_keys.d`` of core and the account: dk manages every file in them."""
        return [keys_dir("core"), keys_dir(self.user)]

    @property
    def masked_units(self) -> list[str]:
        return list(self.manifest.get("masked_units") or [])

    def user_scope(self, dest: str) -> bool:
        """Is ``dest`` read by the account's systemd (else by the system's)?"""
        return dest.startswith(self.quadlet_dir + "/") or dest.startswith(self.home + "/")


def load_committed(gen_dir: Path) -> Committed:
    path = gen_dir / MANIFEST
    if not path.is_file():
        raise HostError(f"{path} is missing: run `dk <env> render` and commit")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for key in ("install", "config_dir", "user", "uid"):
        if key not in manifest:
            raise HostError(f"{path} has no {key!r}: render again with this dk")
    files = {}
    for entry in manifest["install"]:
        src = gen_dir / entry["file"]
        if not src.is_file():
            raise HostError(f"{src} is in the manifest but missing")
        files[entry["file"]] = src.read_text(encoding="utf-8")
    return Committed(manifest=manifest, files=files)


# --- SSH ------------------------------------------------------------------------------------


def _registry_auth_failure(err: str) -> bool:
    """Podman's text for a pull the account has no (valid) registry login for."""
    low = err.lower()
    return "unable to retrieve auth token" in low or ("unauthorized" in low and "docker://" in low)


@dataclass(frozen=True)
class Host:
    address: str
    port: int = 22
    runner: Runner = run

    def _argv(self, login: str, command: str, *, tty: bool = False) -> list[str]:
        from catalpa_tooling.ssh_reuse import reuse_options

        argv = ["ssh", "-o", "BatchMode=yes", *reuse_options()]
        if tty:
            argv.append("-t")
        if self.port != 22:
            argv += ["-p", str(self.port)]
        return [*argv, f"{login}@{self.address}", command]

    def ssh(
        self, login: str, command: str, *, input: bytes | None = None, check: bool = True
    ) -> subprocess.CompletedProcess:
        r = self.runner(self._argv(login, command), input=input)
        if check and r.returncode != 0:
            err = (r.stderr or b"").decode(errors="replace").strip()
            if "Host key verification failed" in err:
                err += (
                    f"\n(a rebuilt droplet has a new host key: check it's yours, then "
                    f"`ssh-keygen -R {self.address}` and run this again)"
                )
            if _registry_auth_failure(err):
                err += (
                    f"\n(the {login} account on {self.address} isn't logged in to the image registry, "
                    "or its token expired or lacks read:packages; the login lives in "
                    "~/.config/containers/auth.json. For an `fcos.auto_update` env run "
                    "`dk <env> host login`; see docs/FCOS.md, Registry login. The tag itself is fine.)"
                )
            raise HostError(f"ssh {login}@{self.address} failed ({r.returncode}): {err}")
        return r

    def interactive(self, login: str, command: str) -> int:
        return self.stream(login, command, tty=True)

    def stream(self, login: str, command: str, *, tty: bool = False) -> int:
        """Run with this terminal's stdin/stdout/stderr (``tty``: allocate a remote one)."""
        return self.runner(self._argv(login, command, tty=tty), tty=True).returncode

    def root_script(self, script: str) -> str:
        """Run ``script`` as root (``core`` with sudo); returns its stdout."""
        return self.ssh("core", "sudo sh -s", input=script.encode()).stdout.decode()


def host_from_docker_host(docker_host: str | None, override: str | None) -> Host:
    from catalpa_tooling.ssh_known_hosts import _ssh_port_from_docker_host, ssh_host_from_docker_host

    if override:
        return Host(address=override)
    host = ssh_host_from_docker_host(docker_host or "")
    if not host:
        raise HostError("info.yaml has no ssh:// docker_host; pass --host")
    return Host(address=host, port=_ssh_port_from_docker_host(docker_host or ""))


# --- What is on the host --------------------------------------------------------------------


@dataclass(frozen=True)
class HostFile:
    mode: int
    owner: str
    text: str


@dataclass
class HostState:
    files: dict[str, HostFile | None] = field(default_factory=dict)
    dirs: dict[str, tuple[int, int] | None] = field(default_factory=dict)
    links: dict[str, str | None] = field(default_factory=dict)
    key_files: set[str] = field(default_factory=set)
    system_secrets: set[str] = field(default_factory=set)
    secrets: set[str] = field(default_factory=set)


def _timer_links(c: Committed) -> dict[str, str]:
    return {
        f"{c.user_unit_dir}/timers.target.wants/{t}": f"../{t}"
        for t in c.manifest.get("timers") or []
    }


def _unit_path(unit: str) -> str:
    return f"/etc/systemd/system/{unit}"


def state_script(c: Committed) -> str:
    q = shlex.quote
    lines = [
        "set -u",
        "f() { if [ -f \"$1\" ]; then printf 'F\\t%s\\t%s\\t%s\\t' \"$1\" \"$(stat -c %a \"$1\")\" "
        "\"$(stat -c %U \"$1\")\"; base64 -w0 \"$1\"; printf '\\n'; else printf 'M\\t%s\\n' \"$1\"; fi; }",
        "d() { if [ -d \"$1\" ]; then printf 'D\\t%s\\t%s\\t%s\\n' \"$1\" \"$(stat -c %a \"$1\")\" "
        "\"$(stat -c %u \"$1\")\"; else printf 'N\\t%s\\n' \"$1\"; fi; }",
        "l() { if [ -L \"$1\" ]; then printf 'L\\t%s\\t%s\\n' \"$1\" \"$(readlink \"$1\")\"; "
        "else printf 'X\\t%s\\n' \"$1\"; fi; }",
        "k() { [ -d \"$1\" ] || return 0; for x in \"$1\"/* \"$1\"/.[!.]*; do "
        "[ -e \"$x\" ] && printf 'K\\t%s\\n' \"$x\"; done; return 0; }",
    ]
    lines += [f"f {q(e['dest'])}" for e in c.manifest["install"]]
    lines += [f"d {q(v['path'])}" for v in (c.manifest.get("bind_volumes") or {}).values()]
    lines += [f"l {q(p)}" for p in _timer_links(c)]
    lines += [f"l {q(_unit_path(u))}" for u in c.masked_units]
    lines += [f"k {q(d)}" for d in c.key_dirs]
    if c.manifest.get("system_secrets"):
        lines.append("podman secret ls --format '{{.Name}}' | while read -r s; do printf 'S\\t%s\\n' \"$s\"; done")
    return "\n".join(lines) + "\n"


def parse_state(out: str) -> HostState:
    s = HostState()
    for line in out.splitlines():
        parts = line.split("\t")
        kind = parts[0]
        if kind == "F" and len(parts) == 5:
            s.files[parts[1]] = HostFile(
                mode=int(parts[2], 8),
                owner=parts[3],
                text=base64.b64decode(parts[4]).decode("utf-8", errors="replace"),
            )
        elif kind == "M":
            s.files[parts[1]] = None
        elif kind == "D":
            s.dirs[parts[1]] = (int(parts[2], 8), int(parts[3]))
        elif kind == "N":
            s.dirs[parts[1]] = None
        elif kind == "L":
            s.links[parts[1]] = parts[2]
        elif kind == "X":
            s.links[parts[1]] = None
        elif kind == "K":
            s.key_files.add(parts[1])
        elif kind == "S" and len(parts) == 2:
            s.system_secrets.add(parts[1])
    return s


def read_host(host: Host, c: Committed) -> HostState:
    state = parse_state(host.root_script(state_script(c)))
    if c.manifest.get("secrets"):
        out = host.ssh(c.user, "podman secret ls --format '{{.Name}}'").stdout.decode()
        state.secrets = {s.strip() for s in out.splitlines() if s.strip()}
    return state


# --- Plan -----------------------------------------------------------------------------------


@dataclass
class Change:
    dest: str
    file: str
    owner: str
    mode: int
    why: str  # "new", "changed", "comments only", "mode …", "owner …"
    old: str | None = None
    new: str = ""

    @property
    def restarts(self) -> bool:
        """Whether a running unit could behave differently after this change."""
        return self.why != COMMENTS_ONLY


COMMENTS_ONLY = "comments only"


def _without_comments(text: str) -> list[str]:
    """``text`` minus its whole-line comments (``#``, and ``;`` as systemd allows), keeping a
    first-line shebang. Every file dk renders (units, quadlets, configs, shell, key files)
    treats those lines as inert."""
    out = []
    for i, line in enumerate(text.splitlines()):
        s = line.lstrip()
        if (s.startswith("#") and not (i == 0 and s.startswith("#!"))) or s.startswith(";"):
            continue
        out.append(line)
    return out


def comments_only(old: str, new: str) -> bool:
    return old != new and _without_comments(old) == _without_comments(new)


@dataclass
class Plan:
    changes: list[Change] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    make_dirs: list[tuple[str, int, int]] = field(default_factory=list)
    dir_problems: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    stray_keys: list[str] = field(default_factory=list)
    masks: list[str] = field(default_factory=list)
    secrets_missing: list[str] = field(default_factory=list)
    secrets_extra: list[str] = field(default_factory=list)
    system_secrets_missing: list[str] = field(default_factory=list)
    system_secrets_extra: list[str] = field(default_factory=list)

    @property
    def files_in_sync(self) -> bool:
        return not (
            self.changes or self.remove or self.make_dirs or self.links or self.stray_keys or self.masks
        )

    @property
    def secrets_in_sync(self) -> bool:
        return not (self.secrets_missing or self.system_secrets_missing)


def make_plan(c: Committed, state: HostState) -> Plan:
    p = Plan()
    for e in sorted(c.manifest["install"], key=lambda e: e["dest"]):
        want_mode = int(e["mode"], 8)
        new = c.files[e["file"]]
        have = state.files.get(e["dest"])
        args = dict(dest=e["dest"], file=e["file"], owner=e["owner"], mode=want_mode, new=new)
        if have is None:
            p.changes.append(Change(why="new", **args))
        elif have.text != new:
            same_perms = have.mode == want_mode and have.owner == e["owner"]
            why = COMMENTS_ONLY if same_perms and comments_only(have.text, new) else "changed"
            p.changes.append(Change(why=why, old=have.text, **args))
        elif have.mode != want_mode:
            p.changes.append(Change(why=f"mode {have.mode:o} → {want_mode:o}", old=have.text, **args))
        elif have.owner != e["owner"]:
            p.changes.append(Change(why=f"owner {have.owner} → {e['owner']}", old=have.text, **args))

    applied = state.files.get(c.manifest_dest)
    if applied is not None:
        try:
            before = json.loads(applied.text)
        except ValueError:
            before = {}
        wanted = {e["dest"] for e in c.manifest["install"]}
        p.remove = sorted({e["dest"] for e in before.get("install") or []} - wanted)

    for name, v in sorted((c.manifest.get("bind_volumes") or {}).items()):
        if "owner_uid" not in v:
            continue
        have = state.dirs.get(v["path"])
        if have is None:
            p.make_dirs.append((v["path"], int(v["mode"], 8), int(v["owner_uid"])))
        elif have[1] != int(v["owner_uid"]):
            # Never chown data: a wrong owner means the ID range moved, which needs a person.
            p.dir_problems.append(
                f"{v['path']} ({name}) is owned by {have[1]}, the render says {v['owner_uid']}: "
                "the ID range changed? Left alone"
            )
    for link, target in sorted(_timer_links(c).items()):
        if state.links.get(link) != target:
            p.links.append((link, target))

    rendered = {e["dest"] for e in c.manifest["install"]}
    p.stray_keys = sorted(state.key_files - rendered)
    p.masks = [u for u in c.masked_units if state.links.get(_unit_path(u)) != "/dev/null"]

    want = set(c.manifest.get("secrets") or {})
    p.secrets_missing = sorted(want - state.secrets)
    p.secrets_extra = sorted(state.secrets - want)
    want_sys = set(c.manifest.get("system_secrets") or {})
    p.system_secrets_missing = sorted(want_sys - state.system_secrets)
    p.system_secrets_extra = sorted(state.system_secrets - want_sys) if want_sys else []
    return p


def describe(p: Plan, *, show_diff: bool = True) -> list[str]:
    out: list[str] = []
    for ch in p.changes:
        out.append(f"{'+' if ch.why == 'new' else '~'} {ch.dest} ({ch.why})")
        if show_diff and ch.old is not None and ch.old != ch.new:
            out += [
                "    " + line.rstrip("\n")
                for line in difflib.unified_diff(
                    ch.old.splitlines(), ch.new.splitlines(), "host", "render", lineterm="", n=1
                )
            ]
    out += [f"- {d} (no longer rendered)" for d in p.remove]
    out += [f"+ {d}/ (mode {m:o}, owner {u})" for d, m, u in p.make_dirs]
    out += [f"! {msg}" for msg in p.dir_problems]
    out += [f"+ {link} → {t} (enable timer)" for link, t in p.links]
    out += [f"- {k} (key file not rendered: its keys stop working)" for k in p.stray_keys]
    out += [f"+ mask {u}" for u in p.masks]
    out += [f"! secret {s} missing (secrets push)" for s in p.secrets_missing]
    out += [f"! system secret {s} missing (secrets push)" for s in p.system_secrets_missing]
    out += [f"? secret {s} on the host but not rendered (left alone)" for s in p.secrets_extra]
    out += [
        f"? system secret {s} on the host but not rendered (left alone)"
        for s in p.system_secrets_extra
    ]
    return out


# --- Which units to restart -----------------------------------------------------------------


@dataclass
class Restarts:
    user: list[str] = field(default_factory=list)
    system: list[str] = field(default_factory=list)
    user_timers: list[str] = field(default_factory=list)
    start_timers: list[str] = field(default_factory=list)
    sockets: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _quadlets(c: Committed) -> dict[str, tuple[str, str, bool]]:
    """Unit name → (dest, text, user scope) for every ``.container`` the render installs."""
    out = {}
    for e in c.manifest["install"]:
        if e["file"].startswith("quadlets/") and e["file"].endswith(".container"):
            stem = posixpath.basename(e["file"])[: -len(".container")]
            out[stem] = (e["dest"], c.files[e["file"]], c.user_scope(e["dest"]))
    return out


def _sockets(c: Committed) -> set[str]:
    return {
        posixpath.basename(e["file"])[: -len(".socket")]
        for e in c.manifest["install"]
        if e["file"].startswith("systemd/") and e["file"].endswith(".socket")
    }


def long_running(c: Committed, stem: str) -> str | None:
    """The unit to restart so container ``stem`` picks up a change: itself when it runs all the
    time (``[Install]``, or socket-activated), the unit it is ``PartOf`` (migrate → django),
    or none (a one-shot a timer starts)."""
    quadlets = _quadlets(c)
    if stem not in quadlets:
        return None
    text = quadlets[stem][1]
    if "\n[Install]" in text or stem in _sockets(c):
        return f"{stem}.service"
    m = re.search(r"^PartOf=(\S+)", text, re.M)
    return m.group(1) if m else None


def restarts_for(c: Committed, dests: list[str], *, secrets: list[str] = ()) -> Restarts:
    """Units to restart after ``dests`` changed (and/or ``secrets`` were replaced)."""
    r = Restarts()
    quadlets = _quadlets(c)
    by_dest = {e["dest"]: e["file"] for e in c.manifest["install"]}
    stems: set[str] = set()
    timers = set(c.manifest.get("timers") or [])
    for dest in dests:
        file = by_dest.get(dest, "")
        base = posixpath.basename(file)
        if file.startswith("quadlets/") and base.endswith(".container"):
            stems.add(base[: -len(".container")])
        elif file.startswith("quadlets/"):
            r.notes.append(
                f"{base} changed: Podman keeps the existing {base.rsplit('.', 1)[-1]} until it is "
                "removed by hand (stop its users, `podman network/volume rm`, start them)"
            )
        elif base.endswith(".timer"):
            (r.user_timers if base in timers else r.notes).append(base)
        elif posixpath.dirname(dest).endswith(".timer.d") and c.user_scope(dest):
            # A drop-in that moves a timer (fcos.auto_update_at): restart that timer, so the next
            # run follows the new schedule. Added or removed alike.
            timer = posixpath.basename(posixpath.dirname(dest))[: -len(".d")]
            if timer not in r.user_timers:
                r.user_timers.append(timer)
        elif posixpath.dirname(dest).endswith(".service.d"):
            pass  # applies the next time the unit starts; never restart a run for it
        elif base.endswith(".socket"):
            r.sockets.append(base[: -len(".socket")])
        elif file == MANIFEST or file == "host.bu":
            pass
        else:
            stems |= {s for s, (_d, text, _u) in quadlets.items() if dest in text}
    for name in secrets:
        stems |= {s for s, (_d, text, _u) in quadlets.items() if f"Secret={name}," in text}
    for stem in sorted(stems):
        unit = long_running(c, stem)
        if unit is None:
            continue
        scope = r.user if quadlets[stem][2] else r.system
        if unit not in scope:
            scope.append(unit)
    r.user.sort()
    r.system.sort()
    return r


def stack_units(c: Committed) -> tuple[list[str], list[str]]:
    """(account units, system units) that run all the time: what ``provision`` starts."""
    user, system = set(), set()
    for stem, (_dest, _text, is_user) in _quadlets(c).items():
        unit = long_running(c, stem)
        if unit == f"{stem}.service":
            (user if is_user else system).add(unit)
    return sorted(user), sorted(system)


# --- Apply ----------------------------------------------------------------------------------


def _parents(dest: str, owner: str) -> list[str]:
    """``owner``'s directories above ``dest`` that may need creating, top first (Ignition
    and ``install -d`` would make them root's)."""
    if owner in ("root", ""):
        return []
    out = []
    d = posixpath.dirname(dest)
    while d.startswith(home_of(owner) + "/"):
        out.append(d)
        d = posixpath.dirname(d)
    return list(reversed(out))


def apply_script(c: Committed, p: Plan) -> str:
    q = shlex.quote
    lines = ["set -eu", "umask 022"]
    made: set[str] = set()
    for ch in p.changes:
        for d in _parents(ch.dest, ch.owner):
            if d not in made:
                made.add(d)
                lines.append(
                    f"[ -d {q(d)} ] || {{ install -d -m {dir_mode(d)} -o {ch.owner} -g {ch.owner} "
                    f"{q(d)}; restorecon {q(d)} 2>/dev/null || true; }}"
                )
        parent = posixpath.dirname(ch.dest)
        if ch.owner in ("root", "") and parent not in made:
            made.add(parent)
            lines.append(f"[ -d {q(parent)} ] || install -d -m 0755 {q(parent)}")
        owner = "root" if ch.owner in ("root", "") else ch.owner
        payload = base64.b64encode(ch.new.encode()).decode()
        lines += [
            f't=$(mktemp {q(ch.dest + ".dk-XXXXXX")})',
            f"printf %s {q(payload)} | base64 -d > \"$t\"",
            f'chown {owner}:{owner} "$t"',
            f'chmod {ch.mode:o} "$t"',
            f'mv -f "$t" {q(ch.dest)}',
            f"restorecon {q(ch.dest)} 2>/dev/null || true",
        ]
    # A tmpfiles.d file makes its directories at boot; a running host needs them now (the
    # read-only Podman socket's, when monitoring is turned on).
    for ch in p.changes:
        if ch.dest.startswith("/etc/tmpfiles.d/") and ch.dest.endswith(".conf"):
            lines.append(f"systemd-tmpfiles --create {q(ch.dest)}")
    for dest in p.remove + p.stray_keys:
        lines.append(f"rm -f {q(dest)}")
    for unit in p.masks:
        lines.append(f"systemctl mask {q(unit)}")
    for path, mode, uid in p.make_dirs:
        lines.append(f"install -d -m {mode:o} -o {uid} -g {uid} {q(path)}")
    for link, target in p.links:
        lines += [
            f"[ -d {q(posixpath.dirname(link))} ] || install -d -m 0755 -o {c.user} -g {c.user} "
            f"{q(posixpath.dirname(link))}",
            f"ln -sfn {q(target)} {q(link)}",
            f"chown -h {c.user}:{c.user} {q(link)}",
        ]
    return "\n".join(lines) + "\n"


def system_reload_needed(c: Committed, p: Plan) -> bool:
    return any(
        not c.user_scope(d) and (d.startswith("/etc/containers/systemd/") or d.startswith("/etc/systemd/system/"))
        for d in [ch.dest for ch in p.changes] + p.remove
    )


def user_reload_needed(c: Committed, p: Plan) -> bool:
    return any(
        d.startswith(c.quadlet_dir + "/") or d.startswith(c.user_unit_dir + "/")
        for d in [ch.dest for ch in p.changes] + p.remove + [link for link, _ in p.links]
    )


def restart_commands(c: Committed, p: Plan, r: Restarts, *, restart: bool) -> tuple[str, str]:
    """(root script, account script) that reload and restart after the files are in place."""
    root: list[str] = []
    if system_reload_needed(c, p):
        root.append("systemctl daemon-reload")
    if restart and r.system:
        root.append("systemctl restart " + " ".join(r.system))
    user: list[str] = []
    if user_reload_needed(c, p):
        user.append("systemctl --user daemon-reload")
    if restart:
        for sock in r.sockets:
            user.append(
                f"systemctl --user stop {sock}.service && systemctl --user restart {sock}.socket "
                f"&& systemctl --user start {sock}.service"
            )
        if r.user:
            user.append("systemctl --user restart " + " ".join(r.user))
        if r.user_timers:
            user.append("systemctl --user restart " + " ".join(sorted(r.user_timers)))
    new_timers = sorted(posixpath.basename(link) for link, _ in p.links)
    if new_timers:
        user.append("systemctl --user start " + " ".join(new_timers))
    return "\n".join(root), "\n".join(user)


# --- Secrets --------------------------------------------------------------------------------


def secret_values(
    c: Committed, credentials: dict, names: list[str] | None
) -> tuple[list[tuple[str, str, bool]], list[str]]:
    """([(secret name, value, system?)], problems), without ever printing a value."""
    by_key = {str(k).upper(): v for k, v in credentials.items() if k != "sops"}
    wanted = [(n, k, False) for n, k in sorted((c.manifest.get("secrets") or {}).items())]
    wanted += [(n, k, True) for n, k in sorted((c.manifest.get("system_secrets") or {}).items())]
    if names:
        unknown = sorted(set(names) - {n for n, _k, _s in wanted})
        if unknown:
            return [], [f"not in the manifest: {', '.join(unknown)}"]
        wanted = [w for w in wanted if w[0] in names]
    out, problems = [], []
    for name, key, system in wanted:
        value = by_key.get(key.upper())
        if value is None or isinstance(value, dict | list):
            problems.append(f"{name}: credentials.yaml has no {key.lower()}{_makes(key)}")
        elif str(value) == "":
            problems.append(f"{name}: {key.lower()} is empty (Podman refuses an empty secret)")
        else:
            out.append((name, str(value), system))
    return out, problems


#: Credentials dk makes itself: which command, by prefix.
_MADE_BY = {
    "PGBR_S3_WRITE_": "backups: [db]; `db configure stanza-create` makes them",
    "RESTIC_WRITE_": "backups: [files]; `files init` makes them",
    "PGBR_S3_READ_": "restore_from; `secrets read-key` makes them",
    "RESTIC_READ_": "restore_from; `secrets read-key` makes them",
}


def _makes(key: str) -> str:
    for prefix, how in _MADE_BY.items():
        if key.upper().startswith(prefix):
            return f" ({how})"
    return ""


#: Reads ``<name> <base64 value>`` lines on stdin and makes each a Podman secret. The values reach
#: ``podman secret create`` through a pipe from ``printf`` (a shell builtin): never on a command
#: line. Prints ``pushed <name>`` for each, so a failure says how far it got.
PUSH_SECRETS_SCRIPT = (
    "set -e\n"
    "while IFS=' ' read -r name value; do\n"
    '  [ -n "$name" ] || continue\n'
    '  printf %s "$value" | base64 -d | podman secret create --replace "$name" - >/dev/null\n'
    '  echo "pushed $name"\n'
    "done\n"
)


def push_secrets(host: Host, c: Committed, values: list[tuple[str, str, bool]]) -> None:
    """Create or replace each secret: one SSH connection for the account's, one for the system's
    (as root), the values on stdin, base64-encoded one per line."""
    for system in (False, True):
        batch = [(n, v) for n, v, s in values if s is system]
        if not batch:
            continue
        for name, _v in batch:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                raise HostError(f"secret name {name!r}: only letters, digits, _ . -")
        stdin = "".join(f"{n} {base64.b64encode(v.encode()).decode()}\n" for n, v in batch).encode()
        cmd = "sh -c " + shlex.quote(PUSH_SECRETS_SCRIPT)
        r = host.ssh("core", f"sudo {cmd}", input=stdin) if system else host.ssh(c.user, cmd, input=stdin)
        done = {line[len("pushed "):] for line in r.stdout.decode().splitlines() if line.startswith("pushed ")}
        for name, _v in batch:
            print(f"  {'system ' if system else ''}secret {name}: {'pushed' if name in done else 'NOT pushed'}", file=sys.stderr)
        missing = [n for n, _v in batch if n not in done]
        if missing:
            raise HostError(f"secrets not pushed: {', '.join(missing)}")


# --- Ignition -------------------------------------------------------------------------------

BUTANE_IMAGE = "quay.io/coreos/butane:release"
#: DigitalOcean's user-data limit.
USER_DATA_LIMIT = 64 * 1024


def compile_ignition(gen_dir: Path, *, runner: Runner = run) -> bytes:
    """``host.bu`` → Ignition, with Butane in a container (``--strict``: a warning fails)."""
    r = runner(
        # label=disable: on an SELinux workstation (Fedora, Podman's docker) a plain mount of
        # the user's files is unreadable in the container; relabelling them would be worse.
        ["docker", "run", "--rm", "--security-opt", "label=disable",
         "-v", f"{gen_dir.resolve()}:/work:ro", "-w", "/work",
         BUTANE_IMAGE, "--strict", "--files-dir", ".", "host.bu"],
    )  # fmt: skip
    if r.returncode != 0:
        raise HostError("butane failed:\n" + (r.stderr or b"").decode(errors="replace").strip())
    if len(r.stdout) > USER_DATA_LIMIT:
        raise HostError(
            f"Ignition is {len(r.stdout)} bytes; DigitalOcean takes at most {USER_DATA_LIMIT}"
        )
    return r.stdout


# --- Registry login -------------------------------------------------------------------------


def private_registries(c: Committed) -> list[str]:
    """Registries the account's images come from that aren't Docker Hub (which needs no login
    for public images)."""
    regs = set()
    for image in (c.manifest.get("images") or {}).values():
        first = image.split("/", 1)[0]
        if "." in first and first != "docker.io":
            regs.add(first)
    return sorted(regs)


def auth_file(c: Committed) -> str:
    """The host.bu drop-in points the account's systemd here (REGISTRY_AUTH_FILE)."""
    return f"{c.home}/.config/containers/auth.json"


def login(host: Host, c: Committed, registry: str, user: str, token: str) -> int:
    """``podman login`` as the account, the token on stdin (never in argv or the output)."""
    r = host.ssh(
        c.user,
        f"podman login --authfile {shlex.quote(auth_file(c))} --username {shlex.quote(user)} "
        f"--password-stdin {shlex.quote(registry)} >/dev/null",
        input=token.encode(),
        check=False,
    )
    if r.returncode != 0:
        err = (r.stderr or b"").decode(errors="replace").replace(token, "<token>").strip()
        print(f"  {registry}: {err}", file=sys.stderr)
    return r.returncode


def logged_in(host: Host, c: Committed, registry: str) -> bool:
    r = host.ssh(
        c.user,
        f"podman login --get-login --authfile {shlex.quote(auth_file(c))} {shlex.quote(registry)}",
        check=False,
    )
    return r.returncode == 0


# --- Not locking the admins out -------------------------------------------------------------


def key_blobs(text: str) -> set[str]:
    """The base64 part of each public key in ``text`` (comments and options ignored)."""
    out = set()
    for line in text.splitlines():
        for part in line.split():
            if part.startswith("AAAA"):
                out.add(part)
                break
    return out


def local_public_keys(*, runner: Runner = run, home: Path | None = None) -> set[str]:
    """The public keys this machine could log in with: the agent's and ``~/.ssh/*.pub``."""
    blobs = set()
    r = runner(["ssh-add", "-L"])
    if r.returncode == 0:
        blobs |= key_blobs((r.stdout or b"").decode(errors="replace"))
    for pub in sorted((home or Path.home()).joinpath(".ssh").glob("*.pub")):
        try:
            blobs |= key_blobs(pub.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return blobs


def core_keys_change(c: Committed, p: Plan) -> bool:
    core_dir = keys_dir("core") + "/"
    return any(ch.dest.startswith(core_dir) and ch.why != COMMENTS_ONLY for ch in p.changes) or any(
        k.startswith(core_dir) for k in p.stray_keys
    )


def lockout_problem(c: Committed, p: Plan, local: set[str]) -> str | None:
    """Why applying ``p`` could lock this machine out of ``core``, or None."""
    if not core_keys_change(c, p):
        return None
    admin = key_blobs(c.files.get("ssh/core.keys", ""))
    if local & admin:
        return None
    if not local:
        return (
            "this changes core's keys, and no public key was found here (ssh-add -L, "
            "~/.ssh/*.pub) to check you keep access"
        )
    return (
        "this changes core's keys, and none of your public keys (ssh-add -L, ~/.ssh/*.pub) is "
        "in fcos.admin_keys: you would lose root on the host"
    )
