"""#233: a wipe dry run needs no TTY; a real wipe without one still refuses."""

import argparse
import sys
from unittest.mock import MagicMock

import pytest

from catalpa_tooling import env_handlers


class _PastTheGuard(Exception):
    pass


def _run(monkeypatch, *, dry_run: bool) -> int:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    def stop(_args):
        raise _PastTheGuard

    monkeypatch.setattr(env_handlers, "_insert_down_remove_orphans", stop)
    ns = argparse.Namespace(yes=False, dry_run=dry_run)
    return env_handlers._run_compose_path(
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


def test_a_wipe_dry_run_does_not_need_a_tty(monkeypatch) -> None:
    with pytest.raises(_PastTheGuard):
        _run(monkeypatch, dry_run=True)


def test_a_real_wipe_without_a_tty_still_refuses(monkeypatch, capsys) -> None:
    assert _run(monkeypatch, dry_run=False) == 1
    assert "without a TTY" in capsys.readouterr().err
