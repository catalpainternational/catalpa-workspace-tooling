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
``down``, ``run``, ``pull`` …) stay refused; ``dk <env> apply`` is how a change reaches
the host.
"""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path
import sys
from dataclasses import dataclass, field

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.fcos.host import Committed, Host, HostError, load_committed, run, stack_units
from catalpa_tooling.fcos.render import generated_dir

UNIT_VERBS = ("restart", "stop", "start")
SUPPORTED = ("logs", "ps", "exec", "manage", *UNIT_VERBS, "db", "files", "pull_media")
DB_COMMANDS = ("info", "check", "version", "backup", "pgdump", "configure", "restore")
FILES_COMMANDS = ("backup", "snapshots", "check", "stats", "init", "restore")
#: What the render owns on a quadlet env, and what to use instead.
BACKUP_REFUSED = {
    "init": "the render installs pgBackRest's config and units: `dk {env} db configure stanza-create` creates the stanza",
    "install-systemd": "the render installs the backup units and timers (`dk {env} apply`)",
    "push": "copy media into the media directory on the host, or restore it with restic",
}


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
    info: dict = field(default_factory=dict)

    @property
    def ssh_target(self) -> str:
        return f"{self.login}@{self.host.address}"

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

    # --- backups ------------------------------------------------------------------------------

    def _installed(self, prefix: str, suffix: str) -> str | None:
        """Unit name of the one rendered file ``prefix…suffix`` (e.g. ``systemd/…@.service``)."""
        for e in self.committed.manifest["install"]:
            f = e["file"]
            if f.startswith(prefix) and f.endswith(suffix):
                return f.rsplit("/", 1)[-1]
        return None

    def _start_and_show(self, unit: str) -> int:
        """Start a one-shot unit, wait for it, then show what it logged (failures included)."""
        q = shlex.quote(unit)
        print(f"{self.host.address}: systemctl --user start {unit}", file=sys.stderr)
        return self._ssh(
            f't=$(date +%s); systemctl --user start {q}; rc=$?; '
            f'journalctl --user -u {q} --since "@$t" --no-pager -o cat '
            # Podman's own event records ("… m=+0.09 container exec …") land in the unit's log.
            f"| grep -vE ' m=\\+[0-9.]+ (container|image|pod|volume|network) ' ; exit $rc",
            tty=False,
        )

    # --- read mode (restore_from, #204) ----------------------------------------------------------

    @property
    def restore(self) -> dict:
        """The manifest's ``restore``: one-off containers with the read credentials."""
        return self.committed.manifest.get("restore") or {}

    def _read_only(self, what: str) -> Usage:
        src = self.restore.get("from") or "another env"
        return Usage(
            f"dk {self.env_name} {what}: {self.env_name} restores {src}'s backups with a read key "
            "and has no repository of its own to write to"
        )

    def _db_read_args(self) -> list[str]:
        """``podman run`` options for a one-off like db, with the read credentials instead of
        db's own pgBackRest settings."""
        db = self.config.stack.services.db
        text = self._db_quadlet()
        kept = "\n".join(
            ln for ln in text.splitlines()
            if not (ln.startswith(("Secret=", "Environment=")) and "PGBACKREST_" in ln)
        )  # fmt: skip
        if not self.restore.get("pgbackrest"):
            raise Usage(f"{self.env_name}'s render has no pgBackRest read credentials ({db})")
        return podman_run_args(
            kept + "\n[Container]\n" + "\n".join(self.restore["pgbackrest"]),
            volumes=volume_names(self.committed),
        )

    def _db_quadlet(self) -> str:
        db = self.config.stack.services.db
        quadlets = {f.rsplit("/", 1)[-1]: t for f, t in self.committed.files.items() if f.startswith("quadlets/")}
        text = quadlets.get(f"{db}.container")
        if not text:
            raise Usage(f"{self.env_name}'s render has no {db}.container")
        return text

    def db(self, sub: str, ns: argparse.Namespace) -> int:
        db = self.config.stack.services.db
        if self.restore.get("pgbackrest"):
            if sub not in ("info", "version", "pgdump"):
                raise self._read_only(f"db {sub}")
            if sub != "pgdump":
                cmd = ["podman", "run", "--rm", "--user", "postgres", "--entrypoint", "pgbackrest",
                       *self._db_read_args(), "--log-level-console=info", sub]  # fmt: skip
                return self._ssh(shlex.join(cmd), tty=False)
        self._check_services([db])
        pgbr = ["podman", "exec", "-u", "postgres", db, "pgbackrest"]
        if sub == "version":
            return self._ssh(shlex.join([*pgbr, "version"]), tty=False)
        if sub in ("info", "check"):
            return self._ssh(shlex.join([*pgbr, "--log-level-console=info", sub]), tty=False)
        if sub == "configure":
            mode = getattr(ns, "configure_mode", None)
            if mode == "stanza-create":
                return self._ssh(shlex.join([*pgbr, "--log-level-console=info", "stanza-create"]), tty=False)
            if mode == "verify":
                return self._ssh(shlex.join([*pgbr, "--log-level-console=info", "check"]), tty=False)
            raise Usage(
                f"dk {self.env_name} db configure: the render writes pgBackRest's config; "
                "use `configure stanza-create` or `configure verify`"
            )
        if sub == "backup":
            template = self._installed("systemd/", "pgbackrest-backup@.service")
            if not template:
                raise Usage(f"{self.env_name}'s render has no pgBackRest backup unit")
            return self._start_and_show(template.replace("@.", f"@{ns.backup_type}."))
        if sub == "pgdump":
            from catalpa_tooling.fcos.fetch import pg_dump_command

            args = [a for a in (getattr(ns, "pg_dump_args", None) or []) if a != "--"]
            return self._ssh(pg_dump_command(db, args), tty=False)
        raise Usage(f"dk {self.env_name} db {sub}: not on a quadlet env")

    def _restic_quadlet(self) -> tuple[str, str]:
        """(unit stem, quadlet text) of the rendered restic container; in read mode, ("", the
        restore container's lines)."""
        if self.restore.get("restic"):
            return "", "[Container]\n" + "\n".join(self.restore["restic"])
        for e in self.committed.manifest["install"]:
            f = e["file"]
            text = self.committed.files.get(f, "")
            if f.startswith("quadlets/") and f.endswith(".container") and "\nImage=" in text and "restic" in text.split("\nImage=", 1)[1].split("\n", 1)[0]:
                return f.rsplit("/", 1)[-1][: -len(".container")], text
        raise Usage(f"{self.env_name}'s render has no restic backup (restic_write_* credentials)")

    def _restic_lock_flag(self) -> list[str]:
        """A read key can't write restic's lock object: read mode runs without one, as compose's
        READ mode does. Nothing else writes the source's repository from here."""
        return ["--no-lock"] if self.restore.get("restic") else []

    def files(self, sub: str) -> int:
        stem, text = self._restic_quadlet()
        if not stem and sub in ("backup", "init"):
            raise self._read_only(f"files {sub}")
        if sub == "backup":
            return self._start_and_show(f"{stem}.service")
        args = {"snapshots": ["snapshots"], "check": ["check"], "stats": ["stats"], "init": ["init"]}[sub]
        args = self._restic_lock_flag() + args
        cmd = ["podman", "run", "--rm", "-i", *podman_run_args(text, volumes=volume_names(self.committed)), *args]
        return self._ssh(shlex.join(cmd), tty=False)

    def _confirm(self, what: str, *, yes: bool) -> bool:
        if yes:
            return True
        from catalpa_tooling.cli_confirm import confirm_by_typing_env_name

        print(f"WARNING: {what}\n  Environment: {self.env_name} ({self.host.address})", file=sys.stderr)
        if confirm_by_typing_env_name(self.env_name):
            return True
        print("Cancelled.", file=sys.stderr)
        return False

    def db_restore(self, extra: list[str], *, yes: bool, dry_run: bool) -> int:
        """pgBackRest restore --delta into the data directory, as compose envs do: stop the app
        and db, restore in a one-off container like db, start again (db's unit waits for
        recovery: it is healthy only once Postgres accepts connections)."""
        db = self.config.stack.services.db
        web = self.config.stack_service("web")
        extra = [a for a in extra if a != "--"]
        source = self.restore.get("from")
        if self.restore.get("pgbackrest"):
            run_args = self._db_read_args()
            if any(a.startswith("--type=") for a in extra) and not any(a.startswith("--target-action") for a in extra):
                extra.append("--target-action=promote")  # pgBackRest's default pauses: we wait for the end
        else:
            text = self._db_quadlet()
            if "PGBACKREST_" not in text:
                raise Usage(f"{self.env_name}'s db has no pgBackRest repository (pgbr_s3_write_* credentials)")
            run_args = podman_run_args(text, volumes=volume_names(self.committed))
        inner = (
            'mkdir -p -m 700 "$PGBACKREST_PG1_PATH" && '
            "pgbackrest --log-level-console=info restore --delta"
            + "".join(" " + shlex.quote(a) for a in extra)
        )
        if self.restore.get("pgbackrest"):
            inner += " && " + _replay_wal_here(source or "the source")
        restore = shlex.join(
            ["podman", "run", "--rm", "--user", "postgres", "--entrypoint", "sh", *run_args, "-c", inner]
        )
        w, d = f"{shlex.quote(web)}.service", f"{shlex.quote(db)}.service"
        # Leave the stack as it was found: a host provisioned with --no-start stays stopped, so
        # the media can be restored before the app serves anything.
        script = "\n".join(
            [
                "set -e",
                f"web_was=0; systemctl --user is-active --quiet {w} && web_was=1",
                f"db_was=0; systemctl --user is-active --quiet {d} && db_was=1",
                f"systemctl --user stop {w} {d}",
                restore,
                'if [ "$web_was" = 1 ]; then',
                f"  echo 'Restored. Starting {db} (it replays WAL; healthy once recovery ends), then {web}…' >&2",
                f"  systemctl --user start {w}",
                'elif [ "$db_was" = 1 ]; then',
                f"  echo 'Restored. Starting {db} (it replays WAL; healthy once recovery ends); {web} was stopped and stays so.' >&2",
                f"  systemctl --user start {d}",
                "else",
                (
                    f"  echo 'Restored. {db} and {web} were stopped and stay so: restore the media "
                    f"(dk {self.env_name} files restore), then start them (dk {self.env_name} host provision).' >&2"
                ),
                "  exit 0",
                "fi",
                f"systemctl --user --no-pager status {d} {w} --lines=0 || true",
            ]
        )
        if dry_run:
            print(f"dry-run: on {self.host.address} as {self.login}:\n{script}", file=sys.stderr)
            return 0
        what = f"{source}'s backup" if source else "the backup"
        if not self._confirm(
            f"pgBackRest restore --delta of {what} into {self.env_name}'s database. {web} and "
            f"{db} stop while it runs; what {what} doesn't have is lost."
            + (_PROD_DATA_NOTE.format(source=source) if source else ""),
            yes=yes,
        ):
            return 1
        rc = self._ssh(script, tty=False)
        if rc != 0:
            return rc
        return self._after_db_restore()

    def _after_db_restore(self) -> int:
        """``ops.post_db_restore``, as compose runs them, once the app is up again. A stack left
        stopped can't run them: say which to run after starting it."""
        from catalpa_tooling.fcos.dbdump import post_restore_hooks
        from catalpa_tooling.post_db_restore import _expand_commands, post_db_restore_applies

        if not post_db_restore_applies(self.config, self.env_name):
            return 0
        web = self.config.stack_service("web")
        r = self.host.ssh(self.login, f"systemctl --user is-active --quiet {shlex.quote(web)}.service", check=False)
        if r.returncode == 0:
            return post_restore_hooks(self)
        commands = _expand_commands(
            self.config, self.config.ops.post_db_restore.manage_commands, env_name=self.env_name,
            env_add={}, hook_label="post_db_restore",
        ) or []  # fmt: skip
        if commands or self.config.ops.post_db_restore.db_psql:
            print(
                "post_db_restore hooks not run: the stack is stopped. Once it's up, run "
                + "; ".join(f"`dk {self.env_name} manage {shlex.join(c)}`" for c in commands)
                + (" and the db_psql hooks" if self.config.ops.post_db_restore.db_psql else "")
                + ".",
                file=sys.stderr,
            )
        return 0

    def db_restore_dump(self, archive: Path | None, *, yes: bool, dry_run: bool) -> int:
        """``db restore --file PATH`` / ``--dumps``: replace the app database with a local
        ``pg_dump -Fc`` archive (``--dumps``: the configured ``paths.fetch_db_dump``)."""
        from catalpa_tooling.fcos.dbdump import restore_archive

        path = archive or self.config.fetch_db_dump_path
        if archive is None and self.config.has_metabase_fetch():
            print(
                "db restore --dumps: the Metabase dump isn't restored on quadlet envs yet; "
                "only the app database.",
                file=sys.stderr,
            )
        if dry_run:
            print(f"dry-run: would replace {self.env_name}'s app database with {path}", file=sys.stderr)
            return 0
        if not self._confirm(
            f"replace {self.env_name}'s app database with {path}. The app stops while it runs; "
            "the current data is lost.",
            yes=yes,
        ):
            return 1
        return restore_archive(self, Path(path))

    def files_restore(self, snapshot: str, *, yes: bool, dry_run: bool) -> int:
        """Replace the media with a restic snapshot: each backed-up directory ends up exactly as
        in the snapshot (``--delete``), owned as the backup unit's ID range wrote it."""
        _stem, text = self._restic_quadlet()
        run_args = podman_run_args(text, volumes=volume_names(self.committed), read_write=True)
        paths = [
            ln.split(":", 2)[1]
            for ln in (x.strip()[len("Volume="):] for x in text.splitlines() if x.strip().startswith("Volume="))
            if ln.split(":", 2)[1].startswith("/backup/")
        ]
        if not paths:
            raise Usage("the restic unit mounts nothing under /backup/")
        host_filter = []
        if snapshot == "latest" and self.restore.get("restic"):
            pass  # The source's repository: compose snapshots carry random hostnames; by path.
        elif snapshot == "latest" and self.committed.manifest.get("hostname"):
            host_filter = ["--host", str(self.committed.manifest["hostname"])]
        steps = []
        for i, path in enumerate(paths):
            path_filter = ["--path", path] if snapshot == "latest" and not host_filter else []
            cmd = shlex.join(
                ["podman", "run", "--rm", *run_args, *self._restic_lock_flag(), "restore", f"{snapshot}:{path}",
                 "--target", path, "--delete", *host_filter, *path_filter,
                 # The :z mount labels the files; a snapshot's labels (or an Ubuntu host's lack
                 # of them) mustn't be written back, which SELinux refuses inside the container.
                 "--exclude-xattr", "security.selinux"]
            )  # fmt: skip
            # A path added after the snapshot was taken isn't in it: keep that one as it is.
            steps.append(cmd if i == 0 else f"{cmd} || echo 'Snapshot has no {path}: left as it is.' >&2")
        script = "set -e\n" + "\n".join(steps)
        if dry_run:
            print(f"dry-run: on {self.host.address} as {self.login}:\n{script}", file=sys.stderr)
            return 0
        if not self._confirm(
            f"replace {', '.join(p.removeprefix('/backup/') for p in paths)} with restic snapshot "
            f"{snapshot!r}: files not in the snapshot are deleted.",
            yes=yes,
        ):
            return 1
        return self._ssh(script, tty=False)

    # --- media ------------------------------------------------------------------------------

    def media_dir(self) -> str:
        from catalpa_tooling.fcos.fetch import media_path

        try:
            return media_path(self.config, self)
        except ValueError as exc:
            raise Usage(str(exc)) from None

    def media_owner_ns_uid(self) -> int:
        """The media's owner as ``podman unshare`` sees it: the range start (ns uid N is host
        uid subuid_start + N - 1, and the range's root is host uid subuid_start + start - 1)."""
        from catalpa_tooling.restic_files import restic_data_volume_key

        vol = (self.committed.manifest.get("bind_volumes") or {})[restic_data_volume_key(self.config)]
        return int(self.committed.manifest["id_ranges"][vol["id_range"]]["start"])

    def pull_media(self, target: Path) -> int:
        """Rsync the media directory to ``target`` (as ``fetch media``)."""
        from catalpa_tooling.media_rsync import rsync_pull_remote_to_local

        remote = self.media_dir()
        print(f"pull_media: rsync {self.ssh_target}:{remote}/ → {target}", file=sys.stderr)
        return rsync_pull_remote_to_local(self.ssh_target, f"{remote}/", target)

    def push_media(self, source: Path, *, dry_run: bool = False) -> int:
        """Rsync ``source/`` over the media directory (``--delete``), then hand it to the media's
        owner (the range's root).

        rsync runs under ``podman unshare``, so the account can write files the range owns.
        It doesn't keep owners (no ``-o``/``-g``: local uids mean nothing there), and the chown
        is a second step on the host, not ``--chown``: macOS's rsync (openrsync) has no
        ``--chown``.
        """
        from catalpa_tooling.media_rsync import format_shell_command
        from catalpa_tooling.run_cmd import run as run_cmd

        uid = self.media_owner_ns_uid()
        media = self.media_dir()
        cmd = [
            "rsync", "-rptDzv", "--progress", "--delete",
            "--rsync-path=podman unshare rsync",
            *(["--dry-run", "--itemize-changes"] if dry_run else []),
            f"{source.resolve()}/", f"{self.ssh_target}:{media}/",
        ]  # fmt: skip
        print(f"$ {format_shell_command(cmd)}", file=sys.stderr)
        if dry_run:
            return run_cmd(cmd, check=False, print_cmd=False).returncode
        # The chown runs even when rsync fails or is interrupted (Ctrl-C): files it already
        # wrote belong to the account's namespace root, and django couldn't write beside them.
        rc = 1
        try:
            rc = run_cmd(cmd, check=False, print_cmd=False).returncode
        finally:
            chown = f"podman unshare chown -R {uid}:{uid} {shlex.quote(media)}"
            print(f"{self.env_name}: {chown}", file=sys.stderr)
            chown_rc = self._ssh(chown, tty=False)
        return rc or chown_rc

    def stop_writers(self) -> list[str]:
        """Stop everything but db (the edge's socket first); returns what to start again."""
        db = f"{self.config.stack.services.db}.service"
        user_units, _system = stack_units(self.committed)
        units = self._proxy_socket() + [u for u in user_units if u != db]
        if units:
            print(f"{self.env_name}: systemctl --user stop {' '.join(units)}", file=sys.stderr)
            self._ssh("systemctl --user stop " + " ".join(shlex.quote(u) for u in units), tty=False)
        return units

    def start_units(self, units: list[str]) -> int:
        if not units:
            return 0
        ordered = [u for u in units if not u.endswith(".socket")] + [u for u in units if u.endswith(".socket")]
        print(f"{self.env_name}: systemctl --user start {' '.join(ordered)}", file=sys.stderr)
        return self._ssh("systemctl --user start " + " ".join(shlex.quote(u) for u in ordered), tty=False)

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


def volume_names(committed: Committed) -> dict[str, str]:
    """``X.volume`` (as a quadlet's ``Volume=`` names it) → the Podman volume it makes."""
    out = {}
    for f, text in committed.files.items():
        if f.startswith("quadlets/") and f.endswith(".volume"):
            unit = f.rsplit("/", 1)[-1]
            name = next(
                (ln.split("=", 1)[1].strip() for ln in text.splitlines() if ln.startswith("VolumeName=")),
                f"systemd-{unit[: -len('.volume')]}",
            )
            out[unit] = name
    return out


_PROD_DATA_NOTE = (
    "\n  This env gets {source}'s data, personal data included, and the app may send email from "
    "it: ops.post_db_restore is where a project deals with that."
)


def _replay_wal_here(source: str) -> str:
    """Shell, in the one-off restore container: replay the source's WAL to its end with the read
    key, promote, stop cleanly. db's own pgBackRest can't reach the source's repository, so
    db's unit must start on a cluster that is already out of recovery."""
    data = '"$PGBACKREST_PG1_PATH"'
    return (
        f"echo 'Replaying {source}'\\''s WAL with the read key (no connections, no archiving)…' >&2 && "
        f"pg_ctl -D {data} -o \"-c listen_addresses='' -c archive_mode=off\" -w -t 3600 start && "
        "{ n=0; until [ \"$(psql -X -At -U postgres -d postgres -c 'select pg_is_in_recovery()' 2>/dev/null)\" = f ]; do "
        "n=$((n+1)); if [ $n -gt 720 ]; then echo 'Still in recovery after an hour: stopping.' >&2; "
        f"pg_ctl -D {data} -m fast -w stop; exit 1; fi; sleep 5; done; }} && "
        f"pg_ctl -D {data} -m fast -w stop && echo 'Recovered and promoted.' >&2"
    )


def podman_run_args(
    quadlet: str, *, volumes: dict[str, str] | None = None, read_write: bool = False
) -> list[str]:
    """``podman run`` options for a one-off container like a rendered ``.container``: its image,
    ID range, settings, secrets and mounts (not its name, network or command). ``volumes``
    resolves ``X.volume`` references; ``read_write`` drops ``ro`` from the mounts."""
    image = None
    out: list[str] = []
    in_container = False
    for line in quadlet.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_container = line == "[Container]"
            continue
        if not in_container or not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "Image":
            image = value
        elif key == "UIDMap":
            out += ["--uidmap", value]
        elif key == "GIDMap":
            out += ["--gidmap", value]
        elif key == "Secret":
            out += ["--secret", value]
        elif key == "Environment":
            out += ["-e", value]
        elif key == "EnvironmentFile":
            out += ["--env-file", value]
        elif key == "Volume":
            source, _, rest = value.partition(":")
            if source.endswith(".volume"):
                if not volumes or source not in volumes:
                    raise Usage(f"volume {source} isn't in the render")
                source = volumes[source]
            if read_write and rest.count(":"):
                target, opts = rest.split(":", 1)
                opts = ",".join(o for o in opts.split(",") if o != "ro")
                rest = f"{target}:{opts}" if opts else target
            out += ["-v", f"{source}:{rest}"]
        elif key == "User":
            out += ["--user", value]
    if image is None:
        raise Usage("the quadlet has no Image=")
    return [*out, image]


REFUSED_HINT = {
    "up": "a bare `dk {env}` brings the host up to date: it applies the committed render, pulls the images and restarts what changed",
    "down": "`dk {env} stop` stops the stack's units",
    "pull": "a bare `dk {env}` pulls the images and restarts the units whose image changed",
    "build": "the host pulls images; build and push them, then `dk {env} render`, commit, and a bare `dk {env}`",
    "run": "`dk {env} exec SERVICE …` runs a command in a running container",
    "rm": "the units remove their containers when they stop",
    "wipe": "deleting a quadlet env's data is a host-level operation, not a dk command",
}


class EnvError(Exception):
    pass


def open_backend(
    config: ProjectConfig, env_name: str, info: dict, *, runner=run, register_key: bool = True
) -> Backend:
    """The backend for a ``runtime: quadlet`` env: its host (from ``docker_host``) and its
    committed render. ``register_key``: record the host's SSH key on first contact, as dk does
    for every deploy host (never replaces one)."""
    from catalpa_tooling.fcos.host import host_from_docker_host
    from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

    docker_host = str(info.get("docker_host") or "")
    try:
        committed = load_committed(generated_dir(config, env_name))
        host = host_from_docker_host(docker_host, None)
        login = parse_docker_host_to_ssh_target(docker_host).split("@", 1)[0]
    except (HostError, ValueError) as exc:
        raise EnvError(str(exc)) from None
    if login != committed.user:
        raise EnvError(
            f"docker_host logs in as {login!r}, but the stack runs as {committed.user!r} (fcos.user)"
        )
    if register_key:
        from catalpa_tooling.ssh_known_hosts import ensure_known_host

        if ensure_known_host(host.address, port=host.port, recovery_env_name=env_name) != 0:
            raise EnvError(f"no SSH host key for {host.address}")
    host = Host(address=host.address, port=host.port, runner=runner)
    return Backend(config=config, env_name=env_name, committed=committed, host=host, login=login, info=info)


def quadlet_backend(config: ProjectConfig, env_name: str, *, runner=run) -> Backend | None:
    """``open_backend`` for ``env_name`` when its info.yaml has ``runtime: quadlet``, else None."""
    import yaml

    from catalpa_tooling.fcos.settings import FcosConfigError, runtime_of

    path = config.deploy_envs_dir / env_name / "info.yaml"
    if not path.is_file():
        return None
    info = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        if runtime_of(info) != "quadlet":
            return None
    except FcosConfigError:
        return None
    return open_backend(config, env_name, info, runner=runner, register_key=runner is run)


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
    elif command == "pull_media":
        verb, argv = "pull_media", []
    elif command in ("db", "bkp_db", "files", "bkp_files"):
        verb = command.removeprefix("bkp_")
        argv = []
    elif command in (None, "compose"):
        argv = list(getattr(ns, "implicit_compose_argv", None) or getattr(ns, "compose_argv", None) or [])
        if not argv and command is None:
            # A bare `dk <env>`: bring the host up to date (compose's `up -d`).
            from catalpa_tooling.fcos.deploy import deploy

            return deploy(
                config, env_name, info,
                yes=bool(getattr(ns, "yes", False)), dry_run=bool(getattr(ns, "dry_run", False)),
                tag=getattr(ns, "tag", None),
            )  # fmt: skip
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
            f"Available: {', '.join(SUPPORTED)}, `render|check|diff|apply`, `host provision`, `secrets push`, and `docker …` against the host's "
            "Podman socket.",
            file=sys.stderr,
        )
        return 2

    dry_run = bool(getattr(ns, "dry_run", False))
    try:
        b = open_backend(config, env_name, info, runner=_dry_runner if dry_run else runner,
                         register_key=runner is run and not dry_run)  # fmt: skip
    except EnvError as exc:
        print(f"dk {env_name} {verb}: {exc}", file=sys.stderr)
        return 1
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
        if verb == "pull_media":
            raw = Path(getattr(ns, "target", None) or f"{config.paths.backend}/media").expanduser()
            target = raw if raw.is_absolute() else config.repo_root / raw
            if dry_run:
                print(f"dry-run: rsync {b.ssh_target}:{b.media_dir()}/ → {target.resolve()}", file=sys.stderr)
                return 0
            return b.pull_media(target.resolve())
        if verb in ("db", "files"):
            sub = getattr(ns, f"{command}_command", None)
            allowed = DB_COMMANDS if verb == "db" else FILES_COMMANDS
            if sub not in allowed:
                hint = BACKUP_REFUSED.get(sub, "not on a quadlet env").format(env=env_name)
                raise Usage(f"dk {env_name} {verb} {sub}: {hint}")
            yes = bool(getattr(ns, "yes", False))
            from catalpa_tooling.fcos.backup_credentials import ensure_backup_credentials, needs_write

            target = needs_write(verb, sub, ns)
            if target:
                rc = ensure_backup_credentials(
                    b, target, yes=yes, dry_run=dry_run, label=f"dk {env_name} {verb} {sub}"
                )
                if rc != 0:
                    return rc
            if verb == "db" and sub == "restore":
                archive = getattr(ns, "archive_file", None)
                if archive or getattr(ns, "restore_from_dumps", False):
                    return b.db_restore_dump(
                        Path(archive).expanduser() if archive else None,
                        yes=yes,
                        dry_run=dry_run or bool(getattr(ns, "restore_dry_run", False)),
                    )
                return b.db_restore(
                    list(getattr(ns, "pgbackrest_restore_args", None) or []),
                    yes=yes,
                    dry_run=dry_run or bool(getattr(ns, "restore_dry_run", False)),
                )
            if verb == "files" and sub == "restore":
                return b.files_restore(getattr(ns, "snapshot", "latest"), yes=yes, dry_run=dry_run)
            return b.db(sub, ns) if verb == "db" else b.files(sub)
        return b.units(verb, argv)
    except Usage as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _dry_runner(argv, *, input=None, tty=False):
    import subprocess

    print("dry-run: " + shlex.join(argv), file=sys.stderr)
    return subprocess.CompletedProcess(argv, 0, b"", b"")


__all__ = ["Backend", "SUPPORTED", "handle", "run"]
