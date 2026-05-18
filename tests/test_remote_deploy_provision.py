"""Tests for dk-only --provision stripping in remote_deploy."""

from catalpa_tooling.remote_deploy import _strip_dk_up_provision_flag


def test_strip_provision_on_up() -> None:
    assert _strip_dk_up_provision_flag(["up", "--provision", "-d"]) == ["up", "-d"]


def test_strip_provision_ignored_on_other_commands() -> None:
    assert _strip_dk_up_provision_flag(["down", "--provision"]) == ["down", "--provision"]
