"""``stack_commands``: ``dk <env> db|files|restore`` written once over ``StackBackend`` (M5)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from catalpa_tooling import db_archive, stack_commands


class FakeBackend:
    """Records what the dispatcher asks of a backend."""

    runtime = "quadlet"
    dump_restore_stops_app = True

    def __init__(self, config, *, pgbackrest: bool = False, refuse: dict | None = None, hooks_problem=None):
        self.config = config
        self.env_name = "staging"
        self.calls: list = []
        self._pgbackrest = pgbackrest
        self._refuse = refuse or {}
        self._hooks_problem = hooks_problem

    def describe(self) -> str:
        return "staging (fake)"

    def refused_subcommand(self, verb, sub):
        return self._refuse.get((verb, sub))

    def ensure_backup_credentials(self, target, **kw):
        self.calls.append(("creds", target))
        return 0

    def pgbackrest(self, sub, ns, *, dry_run):
        self.calls.append(("pgbackrest", sub))
        return 0

    def restic(self, sub, ns, *, dry_run):
        self.calls.append(("restic", sub))
        return 0

    def pgbackrest_restore_available(self):
        return self._pgbackrest

    def pgbackrest_restore(self, extra, *, yes, dry_run):
        self.calls.append(("pgbackrest_restore", extra))
        return 0

    def files_restore(self, snapshot, *, yes, dry_run):
        self.calls.append(("files_restore", snapshot))
        return 0

    def restore_all(self, extra, snapshot, *, yes, dry_run):
        self.calls.append(("restore_all", extra, snapshot))
        return 0

    def restore_hooks_problem(self, *, metabase):
        return self._hooks_problem if metabase else None


@pytest.fixture
def project(minimal_project):
    for env, text in (("staging", "backups: [db, files]\n"), ("prod", "")):
        (minimal_project.deploy_envs_dir / env).mkdir(parents=True, exist_ok=True)
        (minimal_project.deploy_envs_dir / env / "info.yaml").write_text(f"name: {env}\n{text}")
    return minimal_project


@pytest.fixture
def restored(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(
        db_archive, "restore_archive", lambda b, path, *a, target="app", stop_app=False, **k: seen.append((target, path, stop_app)) or 0
    )
    return seen


def _dump(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PGDMP" + b"x" * 200_000)
    return path


def ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(yes=True, **kw)


def test_db_restore_without_pgbackrest_restores_the_configured_dump(project, restored) -> None:
    """Same on both runtimes: no repository to restore from → the local dumps."""
    dump = _dump(project.fetch_db_dump_path)
    b = FakeBackend(project, pgbackrest=False)
    assert stack_commands.run(b, "db", ns(db_command="restore"), dry_run=False) == 0
    assert restored == [("app", dump, True)]  # the backend's stop scope
    assert b.calls == []


def test_db_restore_without_pgbackrest_or_a_dump_fetches_first(project, restored, monkeypatch) -> None:
    from catalpa_tooling import fetch_db

    monkeypatch.setattr(fetch_db, "run_fetch_all_dbs", lambda config, **k: _dump(config.fetch_db_dump_path))
    assert stack_commands.run(FakeBackend(project), "db", ns(db_command="restore"), dry_run=False) == 0
    assert [t for t, _p, _s in restored] == ["app"]


def test_db_restore_dry_run_never_fetches(project, restored, monkeypatch, capsys) -> None:
    from catalpa_tooling import fetch_db

    monkeypatch.setattr(fetch_db, "run_fetch_all_dbs", lambda *a, **k: pytest.fail("fetched in a dry run"))
    assert stack_commands.run(FakeBackend(project), "db", ns(db_command="restore"), dry_run=True) == 0
    assert restored == [] and "would run `dk fetch db`" in capsys.readouterr().err


def test_db_restore_with_pgbackrest_uses_it(project, restored) -> None:
    b = FakeBackend(project, pgbackrest=True)
    args = ns(db_command="restore", pgbackrest_restore_args=["--", "--type=time"])
    assert stack_commands.run(b, "db", args, dry_run=False) == 0
    assert b.calls == [("pgbackrest_restore", ["--type=time"])] and restored == []


def test_dumps_restores_metabase_too_and_checks_its_hooks_first(project, restored, monkeypatch, capsys) -> None:
    """The Metabase dump is restored on every runtime; its hooks must be runnable first."""
    monkeypatch.setattr(type(project), "has_metabase_fetch", lambda self: True)
    monkeypatch.setattr(type(project), "fetch_metabase_db_dump_path", property(lambda self: self.repo_root / "mb.custom"))
    app, mb = _dump(project.fetch_db_dump_path), _dump(project.repo_root / "mb.custom")
    assert stack_commands.run(FakeBackend(project), "db", ns(db_command="restore", restore_from_dumps=True), dry_run=False) == 0
    assert [(t, p) for t, p, _s in restored] == [("app", app), ("metabase", mb)]

    restored.clear()
    b = FakeBackend(project, hooks_problem="render has no django-task.container")
    assert stack_commands.run(b, "db", ns(db_command="restore", restore_from_dumps=True), dry_run=False) == 1
    assert restored == [] and "django-task.container" in capsys.readouterr().err


def test_a_refused_subcommand_says_what_instead(project, capsys) -> None:
    b = FakeBackend(project, refuse={("db", "init"): "the render installs it"})
    assert stack_commands.run(b, "db", ns(db_command="init"), dry_run=False) == 2
    assert "dk staging db init: the render installs it" in capsys.readouterr().err
    assert b.calls == []


def test_a_backup_on_an_env_that_does_not_back_up_is_refused(project, capsys) -> None:
    b = FakeBackend(project)
    b.env_name = "prod"  # no backups:
    assert stack_commands.run(b, "files", ns(files_command="backup"), dry_run=False) == 2
    assert "prod doesn't back up files" in capsys.readouterr().err and b.calls == []


def test_a_backup_creates_missing_credentials_then_runs(project) -> None:
    b = FakeBackend(project)
    assert stack_commands.run(b, "db", ns(db_command="backup", backup_type="incr"), dry_run=False) == 0
    assert b.calls == [("creds", "pgbackrest"), ("pgbackrest", "backup")]
    b.calls.clear()
    assert stack_commands.run(b, "files", ns(files_command="snapshots"), dry_run=False) == 0
    assert b.calls == [("restic", "snapshots")]  # reading needs no write credentials


def test_bkp_spellings_still_work_with_a_warning(project, capsys) -> None:
    b = FakeBackend(project)
    assert stack_commands.run(b, "bkp_files", ns(bkp_files_command="stats"), dry_run=False) == 0
    assert b.calls == [("restic", "stats")] and "Deprecated" in capsys.readouterr().err


def test_restore_goes_to_the_backend(project) -> None:
    b = FakeBackend(project)
    args = ns(pgbackrest_restore_args=["--"], snapshot=" ")
    assert stack_commands.run(b, "restore", args, dry_run=False) == 0
    assert b.calls == [("restore_all", [], "latest")]


def test_which_commands_need_write_credentials() -> None:
    w = stack_commands._write_target
    ns = argparse.Namespace(configure_mode="stanza-create")
    assert w("db", "configure", ns) == "pgbackrest"
    assert w("db", "configure", argparse.Namespace(configure_mode="verify")) is None
    assert w("db", "backup", ns) == "pgbackrest"
    assert w("db", "info", ns) is None
    assert w("db", "restore", ns) is None
    assert w("files", "init", ns) == "restic"
    assert w("files", "backup", ns) == "restic"
    assert w("files", "snapshots", ns) is None


def test_restore_dry_run_after_the_command_is_a_dry_run(project) -> None:
    """`dk <env> restore --dry-run` used to hand --dry-run to pgBackRest and run for real."""
    from catalpa_tooling.dk_cli import _parse_dk
    from catalpa_tooling.dk_parser import build_dk_parser

    parsed = _parse_dk(project, ["staging", "restore", "--dry-run"], build_dk_parser(project))
    assert parsed.restore_dry_run is True and parsed.pgbackrest_restore_args == []
    seen = []

    class B(FakeBackend):
        def restore_all(self, extra, snapshot, *, yes, dry_run):
            seen.append(dry_run)
            return 0

    assert stack_commands.run(B(project), "restore", parsed, dry_run=False) == 0
    assert seen == [True]
