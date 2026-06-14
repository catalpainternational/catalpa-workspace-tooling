"""Parser tree tests for autocomplete refactor."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pytest

from catalpa_tooling.cli.dk_argv import (
    build_implicit_compose_namespace,
    normalize_dk_env_argv,
    normalize_dk_root_argv,
)
from catalpa_tooling.config import load_project_config
from catalpa_tooling.native_parser import build_native_parser
from catalpa_tooling.dk_parser import build_dk_parser
from catalpa_tooling.test_parser import build_test_parser
from tests.helpers import write_minimal_tooling_tree


@pytest.fixture
def tooling_repo(tmp_path: Path) -> Path:
    write_minimal_tooling_tree(tmp_path)
    for env in ("local", "staging"):
        d = tmp_path / "docker" / "envs" / env
        d.mkdir(parents=True)
        (d / "info.yaml").write_text(f"name: {env}\n", encoding="utf-8")
    return tmp_path


def test_dev_parser_fetch_db(tooling_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tooling_repo)
    config = load_project_config(tooling_repo)
    parser, _ = build_native_parser(config)
    ns = parser.parse_args(["fetch", "db", "--env", "staging"])
    assert ns.command == "fetch"
    assert ns.resource == "db"
    assert ns.env == "staging"


def test_test_parser_workspace_remainder() -> None:
    parser = build_test_parser()
    ns = parser.parse_args(["workspace", "tests/test_foo.py", "-k", "bar"])
    assert ns.command == "workspace"
    assert ns.pytest_args == ["tests/test_foo.py", "-k", "bar"]


def test_dk_parser_build_services(tooling_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tooling_repo)
    config = load_project_config(tooling_repo)
    parser = build_dk_parser(config)
    ns = parser.parse_args(["build", "db"])
    assert ns.dk_command == "build"
    assert ns.services == ["db"]


def test_dk_parser_implicit_compose(tooling_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tooling_repo)
    ns = build_implicit_compose_namespace(["local", "up", "-d"])
    assert ns.env_name == "local"
    assert ns.env_command is None
    assert ns.implicit_compose_argv == ["up", "-d"]


def test_dk_parser_explicit_compose(tooling_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tooling_repo)
    config = load_project_config(tooling_repo)
    parser = build_dk_parser(config)
    ns = parser.parse_args(["local", "compose", "logs", "web"])
    assert ns.env_command == "compose"
    assert ns.compose_argv == ["logs", "web"]


def test_dk_parser_env_flags_after_env_name(tooling_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tooling_repo)
    config = load_project_config(tooling_repo)
    parser = build_dk_parser(config)
    argv = normalize_dk_env_argv(["staging", "--tag", "v9", "info"])
    ns = parser.parse_args(argv)
    assert ns.env_name == "staging"
    assert ns.tag == "v9"
    assert ns.env_command == "info"


def test_dk_root_dry_run_before_env(tooling_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tooling_repo)
    config = load_project_config(tooling_repo)
    parser = build_dk_parser(config)
    argv = normalize_dk_root_argv(["--dry-run", "local", "info"])
    ns = parser.parse_args(argv)
    assert ns.env_name == "local"
    assert ns.dry_run is True
    assert ns.env_command == "info"


def test_dk_parser_skips_digoc_env_name_conflict(tooling_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tooling_repo)
    digoc_env = tooling_repo / "docker" / "envs" / "digoc"
    digoc_env.mkdir(parents=True, exist_ok=True)
    (digoc_env / "info.yaml").write_text("name: digoc\n", encoding="utf-8")
    config = load_project_config(tooling_repo)
    parser = build_dk_parser(config)
    ns = parser.parse_args(["digoc", "cloud-config", "print"])
    assert ns.dk_command == "digoc"
    assert ns.digoc_command == "cloud-config"


def test_normalize_dk_env_argv_trailing_yes() -> None:
    assert normalize_dk_env_argv(["demo", "wipe", "--yes"]) == ["demo", "--yes", "wipe"]


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("argcomplete"),
    reason="argcomplete not installed",
)
def test_dev_parser_has_completer_on_fetch_env(tooling_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tooling_repo)
    config = load_project_config(tooling_repo)
    parser, _ = build_native_parser(config)
    fetch_db = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            fetch = action.choices.get("fetch")
            if fetch is None:
                continue
            for sub in fetch._actions:
                if isinstance(sub, argparse._SubParsersAction):
                    db = sub.choices.get("db")
                    if db is not None:
                        for a in db._actions:
                            if a.dest == "env":
                                fetch_db = a
    assert fetch_db is not None
    assert getattr(fetch_db, "completer", None) is not None


@pytest.mark.skipif(
    not __import__("importlib").util.find_spec("argcomplete"),
    reason="argcomplete not installed",
)
def test_dk_argcomplete_env_subcommands(tooling_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Regression: activate() must run before empty-argv help exit."""
    import subprocess

    monkeypatch.chdir(tooling_repo)
    out = tmp_path / "completions"
    env = os.environ.copy()
    env["_ARGCOMPLETE"] = "1"
    env["COMP_LINE"] = "dk local i"
    env["COMP_POINT"] = "9"
    env["COMP_TYPE"] = "9"
    env["_ARGCOMPLETE_STDOUT_FILENAME"] = str(out)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['dk']; from catalpa_tooling.dk_cli import _main_impl; _main_impl()",
        ],
        cwd=tooling_repo,
        env=env,
        check=False,
    )
    assert result.returncode == 0
    words = out.read_text(encoding="utf-8").split("\013")
    assert "info" in words
    assert "compose" in words
