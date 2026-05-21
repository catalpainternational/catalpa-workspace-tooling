"""Tests for dev/scripts CLI script discovery."""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

import pytest

from catalpa_tooling.dev_cli import _dev_main
from catalpa_tooling.scripts_cli import _scripts_main
from tests.helpers import write_minimal_tooling_tree


def test_dev_help_lists_discovered_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_tooling: None
) -> None:
    write_minimal_tooling_tree(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "dev-foo.sh").write_text("#!/usr/bin/env bash\necho foo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["dev", "--help"])
    buf = StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with pytest.raises(SystemExit) as exc:
        _dev_main()
    assert exc.value.code == 0
    assert "foo" in buf.getvalue()


def test_dev_runs_discovered_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_tooling: None
) -> None:
    write_minimal_tooling_tree(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "dev-foo.sh").write_text(
        "#!/usr/bin/env bash\necho \"ran:${1:-none}\"\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["dev", "foo", "bar"])
    with pytest.raises(SystemExit) as exc:
        _dev_main()
    assert exc.value.code == 0


def test_scripts_help_lists_non_dev_scripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_tooling: None
) -> None:
    write_minimal_tooling_tree(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "hello_world.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["scripts", "--help"])
    buf = StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with pytest.raises(SystemExit) as exc:
        _scripts_main()
    assert exc.value.code == 0
    assert "hello-world" in buf.getvalue()
