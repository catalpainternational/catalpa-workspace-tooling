"""Tests for doctl failure message formatting."""

from __future__ import annotations

import subprocess

from catalpa_tooling.doctl_binary import format_doctl_failure


def test_format_doctl_failure_parses_json_errors_on_stdout() -> None:
    result = subprocess.CompletedProcess(
        args=["doctl", "projects", "list"],
        returncode=1,
        stdout='{"errors":[{"detail":"403 forbidden"}]}',
        stderr="",
    )
    assert format_doctl_failure(result) == "403 forbidden"


def test_format_doctl_failure_prefers_stderr() -> None:
    result = subprocess.CompletedProcess(
        args=["doctl"],
        returncode=1,
        stdout='{"errors":[{"detail":"ignored"}]}',
        stderr="from stderr",
    )
    assert format_doctl_failure(result) == "from stderr"
