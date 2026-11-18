"""Tests for ``dk <env> docker`` passthrough."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

from catalpa_tooling.compose import _docker, run_docker_passthrough


def test_docker_merges_env_add_and_builds_argv() -> None:
    completed = subprocess.CompletedProcess(args=["docker"], returncode=0)
    with patch("catalpa_tooling.compose.run_cmd", return_value=completed) as mock_run:
        result = _docker(
            "volume",
            "ls",
            check=False,
            env_add={"DOCKER_HOST": "ssh://root@example", "COMPOSE_PROJECT_NAME": "tvi"},
        )
    assert result.returncode == 0
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd == ["docker", "volume", "ls"]
    env = mock_run.call_args.kwargs["env"]
    assert env["DOCKER_HOST"] == "ssh://root@example"
    assert env["COMPOSE_PROJECT_NAME"] == "tvi"


def test_run_docker_passthrough_strips_leading_double_dash() -> None:
    completed = subprocess.CompletedProcess(args=["docker"], returncode=0)
    with patch("catalpa_tooling.compose._docker", return_value=completed) as mock_docker:
        rc = run_docker_passthrough(
            ["--", "ps", "-a"],
            env_add={"DOCKER_HOST": "ssh://root@h"},
        )
    assert rc == 0
    mock_docker.assert_called_once_with(
        "ps",
        "-a",
        check=False,
        env_add={"DOCKER_HOST": "ssh://root@h"},
    )


def test_run_docker_passthrough_dry_run(capsys) -> None:
    with patch("catalpa_tooling.compose._docker") as mock_docker:
        rc = run_docker_passthrough(
            ["volume", "ls"],
            env_add={"DOCKER_HOST": "ssh://root@h"},
            dry_run=True,
        )
    assert rc == 0
    mock_docker.assert_not_called()
    err = capsys.readouterr().err
    assert "dry-run: docker volume ls" in err
    assert "ssh://root@h" in err


def test_run_docker_passthrough_stdout_is_only_child_output(tmp_path, monkeypatch, capfdbinary) -> None:
    """The ``$ …`` echo goes to stderr, so redirected binary output stays intact (#196)."""
    payload = b"PGDMP\x00\x01\xff binary\n"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write({payload!r})\n"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    rc = run_docker_passthrough(["exec", "db", "pg_dump", "-Fc"], env_add={})

    assert rc == 0
    out, err = capfdbinary.readouterr()
    assert out == payload
    assert b"$ docker exec db pg_dump -Fc" in err
