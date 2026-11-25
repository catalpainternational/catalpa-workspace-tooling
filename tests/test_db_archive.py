"""``db_archive.restore_archive``: one restore sequence over any StackBackend (M5 step 5)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from catalpa_tooling.db_archive import restore_archive


class FakeBackend:
    """Records what ``restore_archive`` asks of a backend; ``fail`` names the step that fails."""

    def __init__(self, config, fail: str | None = None) -> None:
        self.config = config
        self.env_name = "dev"
        self.fail = fail
        self.calls: list = []

    def _rc(self, step: str) -> int:
        return 1 if self.fail == step else 0

    def ensure_db_running(self) -> int:
        self.calls.append("ensure")
        return self._rc("ensure")

    def stop_writers(self) -> object:
        self.calls.append("stop")
        return ["w"]

    def start_writers(self, token: object) -> int:
        self.calls.append(("start", token))
        return 0

    def copy_into_db(self, archive: Path, container_path: str) -> int:
        self.calls.append(("copy", container_path))
        return self._rc("copy")

    def db_sh(self, script: str, *, root: bool = False) -> int:
        kind = "drop_create" if "dropdb" in script else "pg_restore" if "pg_restore" in script else "rm"
        self.calls.append((kind, root))
        return self._rc(kind)

    def run_post_restore_hooks(self, target: str = "app") -> int:
        self.calls.append(f"hooks:{target}")
        return self._rc("hooks")


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    path = tmp_path / "app.dump"
    path.write_bytes(b"PGDMP" + b"x" * 100)
    return path


def _kinds(b: FakeBackend) -> list:
    return [c[0] if isinstance(c, tuple) else c for c in b.calls]


def test_sequence_without_stopping_writers(minimal_project, archive, capsys) -> None:
    b = FakeBackend(minimal_project)
    assert restore_archive(b, archive) == 0
    assert _kinds(b) == ["ensure", "copy", "drop_create", "pg_restore", "rm", "hooks:app"]
    assert ("rm", True) in b.calls  # the copy is removed as root
    assert "db restore: done." in capsys.readouterr().err


def test_stop_app_stops_before_and_starts_after_the_restore_but_before_hooks(minimal_project, archive) -> None:
    b = FakeBackend(minimal_project)
    assert restore_archive(b, archive, stop_app=True) == 0
    assert _kinds(b) == ["stop", "ensure", "copy", "drop_create", "pg_restore", "rm", "start", "hooks:app"]
    assert ("start", ["w"]) in b.calls  # what stop_writers returned


@pytest.mark.parametrize("fail", ["ensure", "copy", "drop_create", "pg_restore"])
def test_a_failed_step_stops_the_sequence_and_still_cleans_up(minimal_project, archive, fail, capsys) -> None:
    b = FakeBackend(minimal_project, fail=fail)
    assert restore_archive(b, archive, stop_app=True) == 1
    kinds = _kinds(b)
    assert kinds[-1] == "start" and "hooks:app" not in kinds  # writers back, no hooks on a failed restore
    assert ("rm" in kinds) == (fail != "ensure")  # nothing copied yet when ensure fails
    assert "db restore: done." not in capsys.readouterr().err


def test_a_non_archive_is_refused_before_anything_runs(minimal_project, tmp_path, capsys) -> None:
    bad = tmp_path / "bad.dump"
    bad.write_bytes(b"-- PostgreSQL database dump\n")
    b = FakeBackend(minimal_project)
    assert restore_archive(b, bad, stop_app=True) == 1
    assert b.calls == []
    assert "db restore:" in capsys.readouterr().err


def test_hooks_can_be_deferred_and_target_selects_the_hook_set(minimal_project, archive) -> None:
    b = FakeBackend(minimal_project)
    assert restore_archive(b, archive, run_hooks=False) == 0
    assert not any(str(k).startswith("hooks") for k in _kinds(b))
    b = FakeBackend(minimal_project)
    assert restore_archive(b, archive, target="metabase") == 0
    assert _kinds(b)[-1] == "hooks:metabase"


def test_a_failing_hook_fails_the_restore(minimal_project, archive, capsys) -> None:
    b = FakeBackend(minimal_project, fail="hooks")
    assert restore_archive(b, archive) == 1
    assert "db restore: done." not in capsys.readouterr().err


def test_compose_backend_db_primitives(monkeypatch: pytest.MonkeyPatch, minimal_project, archive) -> None:
    from catalpa_tooling import pgbackrest_db
    from catalpa_tooling.stack_backend import ComposeBackend

    seen: list[list[str]] = []

    def fake_run_cmd(argv, **kwargs):
        seen.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(pgbackrest_db, "run_cmd", fake_run_cmd)
    b = ComposeBackend.from_parts(minimal_project, "dev", "compose.yml", {"COMPOSE_PROJECT_NAME": "p"})

    assert b.db_sh("select 1") == 0
    assert seen[-1] == ["docker", "compose", "-f", "compose.yml", "exec", "-T", "db", "sh", "-c", "select 1"]
    assert b.db_sh("rm -f /x", root=True) == 0
    assert seen[-1][4:9] == ["exec", "-T", "-u", "root", "db"]

    seen.clear()
    assert b.copy_into_db(archive, "/tmp/x.dump") == 0
    assert seen[0][3:5] == ["compose", "-f"] or seen[0][:4] == ["docker", "compose", "-f", "compose.yml"]
    assert seen[0][4:] == ["cp", str(archive.resolve()), "db:/tmp/x.dump"]
    assert "chown postgres:postgres /tmp/x.dump" in seen[1][-1]  # readable by postgres: root exec
    assert "-u" in seen[1] and "root" in seen[1]
