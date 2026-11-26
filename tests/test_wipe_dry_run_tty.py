"""#233: `--dry-run wipe` removes nothing and needs no TTY; a real wipe without a TTY refuses."""

import argparse
import sys
from unittest.mock import MagicMock

from catalpa_tooling import env_handlers


def _run(monkeypatch, *, dry_run: bool) -> int:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    ran: list[str] = []

    def nothing_may_run(*a, **k):
        ran.append("ran")
        raise AssertionError("a dry run must not run compose or remove volumes")

    monkeypatch.setattr(env_handlers, "_compose", nothing_may_run)
    monkeypatch.setattr(env_handlers, "remove_wipe_data_volumes", nothing_may_run)
    monkeypatch.setattr(env_handlers, "sync_local_proxy_for_compose_action", lambda *a, **k: 0)
    ns = argparse.Namespace(yes=False, dry_run=dry_run)
    rc = env_handlers._run_compose_path(
        ns,
        MagicMock(),
        info={},
        compose_file="compose.yml",
        env_add={},
        env_name="qad",
        compose_args=["wipe"],
        use_prepulled_registry=False,
        site_origin="https://example.test",
        docker_host="ssh://example",
        storage_volumes={},
    )
    assert ran == []
    return rc


def test_a_wipe_dry_run_removes_nothing_and_needs_no_tty(monkeypatch, capsys) -> None:
    assert _run(monkeypatch, dry_run=True) == 0
    err = capsys.readouterr().err
    assert "dry-run: would run: docker compose -f compose.yml down" in err and "-v" in err
    assert "would then remove" in err


def test_a_real_wipe_without_a_tty_still_refuses(monkeypatch, capsys) -> None:
    assert _run(monkeypatch, dry_run=False) == 1
    assert "without a TTY" in capsys.readouterr().err
