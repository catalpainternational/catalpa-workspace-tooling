"""``dk --version`` (#207) and where a trailing ``--dry-run`` goes (#220)."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalpa_tooling import dk_cli, dk_parser


def test_tooling_version_reads_the_installed_metadata() -> None:
    from importlib import metadata

    try:
        installed = metadata.version("catalpa-workspace-tooling")
    except metadata.PackageNotFoundError:
        pytest.skip("not installed as a distribution here")
    assert dk_parser.tooling_version().startswith(installed)


def test_version_flag_prints_and_exits(capsys, monkeypatch, tmp_path) -> None:
    from catalpa_tooling.config import ProjectConfig

    monkeypatch.chdir(Path(__file__).parent / "fixtures" / "minimal_project")
    parser = dk_parser.build_dk_parser(ProjectConfig.from_cwd())
    with pytest.raises(SystemExit) as e:
        parser.parse_args(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.startswith("dk ")


def test_trailing_dry_run_says_where_it_goes(capsys) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="dk")
    sub = parser.add_subparsers(dest="env_command")
    sub.add_parser("pull_media")
    with pytest.raises(SystemExit) as e:
        dk_cli._parse_env_argv(parser, ["pull_media", "--dry-run"])
    assert e.value.code == 2
    assert "--dry-run goes after the env name: dk pull_media --dry-run" in capsys.readouterr().err
