"""Engine resolution for ``dk <env>``: pinned by default, inherited only on opt-in (issue #94)."""

from __future__ import annotations

import pytest

from catalpa_tooling.managed_deploy_env import load_managed_deploy_context, resolve_docker_host

PODMAN = "unix:///var/folders/xx/T/podman/podman-machine-default-api.sock"


def test_local_env_without_ambient_is_default_socket() -> None:
    assert resolve_docker_host({}, "dev", environ={}) == ("", False)


def test_ambient_is_ignored_without_opt_in(capsys: pytest.CaptureFixture[str]) -> None:
    assert resolve_docker_host({}, "dev", environ={"DOCKER_HOST": PODMAN}) == ("", False)
    err = capsys.readouterr().err
    assert PODMAN in err
    assert "dk build" in err
    assert "docker_host_from_env: true" in err


def test_pinned_remote_wins_over_ambient(capsys: pytest.CaptureFixture[str]) -> None:
    info = {"docker_host": "ssh://root@example.org"}
    assert resolve_docker_host(info, "prod", environ={"DOCKER_HOST": PODMAN}) == (
        "ssh://root@example.org",
        False,
    )
    err = capsys.readouterr().err
    assert "info.yaml" in err
    # Opting a deploy env into the shell's engine is never the advice.
    assert "docker_host_from_env: true" not in err


def test_pinned_matching_ambient_is_quiet(capsys: pytest.CaptureFixture[str]) -> None:
    info = {"docker_host": PODMAN}
    assert resolve_docker_host(info, "dev", environ={"DOCKER_HOST": PODMAN}) == (PODMAN, False)
    assert capsys.readouterr().err == ""


def test_opt_in_inherits_ambient(capsys: pytest.CaptureFixture[str]) -> None:
    info = {"docker_host_from_env": True}
    assert resolve_docker_host(info, "dev", environ={"DOCKER_HOST": PODMAN}) == (PODMAN, True)
    assert "inherited from the environment" in capsys.readouterr().err


def test_opt_in_without_ambient_falls_back_to_default_socket() -> None:
    info = {"docker_host_from_env": True}
    assert resolve_docker_host(info, "dev", environ={}) == ("", False)


def test_opt_in_accepts_yaml_truthy_strings() -> None:
    info = {"docker_host_from_env": "yes"}
    assert resolve_docker_host(info, "dev", environ={"DOCKER_HOST": PODMAN}) == (PODMAN, True)


def test_opt_in_with_pinned_host_is_a_config_error(capsys: pytest.CaptureFixture[str]) -> None:
    info = {"docker_host": "ssh://root@example.org", "docker_host_from_env": True}
    assert resolve_docker_host(info, "prod", environ={"DOCKER_HOST": PODMAN}) is None
    assert "Remove one" in capsys.readouterr().err


def _write_env(minimal_project, name: str, body: str) -> None:
    env_dir = minimal_project.deploy_envs_dir / name
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "info.yaml").write_text(body, encoding="utf-8")


def test_context_pins_default_socket_over_ambient(
    minimal_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(
        minimal_project, "local", "name: local\ncredentials_decrypt_optional: true\n"
    )
    monkeypatch.setenv("DOCKER_HOST", PODMAN)
    ctx = load_managed_deploy_context(minimal_project, "local")
    assert ctx is not None
    assert ctx.docker_host == ""
    assert ctx.env_add["DOCKER_HOST"] == ""


def test_context_inherits_ambient_on_opt_in(
    minimal_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(
        minimal_project,
        "local",
        "name: local\ncredentials_decrypt_optional: true\ndocker_host_from_env: true\n",
    )
    monkeypatch.setenv("DOCKER_HOST", PODMAN)
    ctx = load_managed_deploy_context(minimal_project, "local")
    assert ctx is not None
    assert ctx.docker_host == PODMAN
    assert ctx.env_add["DOCKER_HOST"] == PODMAN
    # Proxy / LAN / dc-backup read the engine off info, so it has to carry the inherited value.
    assert ctx.info["docker_host"] == PODMAN
