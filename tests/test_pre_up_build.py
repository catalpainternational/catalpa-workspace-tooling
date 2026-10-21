"""The pre-``up`` stack build, and when the stale-stack hook makes it redundant (#75)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from catalpa_tooling import env_handlers, stale_stack
from catalpa_tooling.config import load_project_config
from catalpa_tooling.stale_stack import pre_up_build_is_redundant, stack_db_image_present
from tests.helpers import write_minimal_tooling_tree

TAG = "my-branch"


@pytest.fixture
def config(tmp_path: Path):
    write_minimal_tooling_tree(tmp_path)
    return load_project_config(tmp_path)


# --- stack_db_image_present ---------------------------------------------------------------


def _fake_docker_for_db(
    monkeypatch: pytest.MonkeyPatch, *, services: dict, images: set[str]
) -> None:
    class _Proc:
        def __init__(self, returncode: int = 0, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **_kwargs):
        if cmd[:3] == ["docker", "compose", "-f"] and "config" in cmd:
            return _Proc(stdout=json.dumps({"services": services}))
        if cmd[:2] == ["docker", "image"]:
            return _Proc(stdout="{}") if cmd[3] in images else _Proc(returncode=1)
        raise AssertionError(f"unexpected docker call: {cmd}")

    monkeypatch.setattr(stale_stack, "run_cmd", fake_run)


def test_db_image_present_when_docker_has_it(config, monkeypatch: pytest.MonkeyPatch) -> None:
    db = config.stack_service("db")
    _fake_docker_for_db(
        monkeypatch,
        services={db: {"image": f"reg/db:{TAG}", "build": {"context": "."}}},
        images={f"reg/db:{TAG}"},
    )
    assert stack_db_image_present(config, "compose.yml", {}) is True


def test_db_image_absent_is_reported(config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The premise the skip rests on: `materialize_configs` runs one-off containers from this
    image before compose's own `--build` could create it, so "missing" has to be detectable."""
    db = config.stack_service("db")
    _fake_docker_for_db(
        monkeypatch,
        services={db: {"image": f"reg/db:{TAG}", "build": {"context": "."}}},
        images=set(),
    )
    assert stack_db_image_present(config, "compose.yml", {}) is False


def test_db_image_absent_when_compose_names_no_db(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_docker_for_db(monkeypatch, services={"web": {"image": "reg/web:x"}}, images=set())
    assert stack_db_image_present(config, "compose.yml", {}) is False


# --- pre_up_build_is_redundant --------------------------------------------------------------


def _env(tag: str = TAG, sha: str = "abc1234") -> dict[str, str]:
    return {"STACK_IMAGE_TAG": tag, "VITE_GIT_SHA": sha}


def _db_present(monkeypatch: pytest.MonkeyPatch, config, present: bool) -> None:
    db = config.stack_service("db")
    _fake_docker_for_db(
        monkeypatch,
        services={db: {"image": f"reg/db:{TAG}", "build": {"context": "."}}},
        images={f"reg/db:{TAG}"} if present else set(),
    )


def test_redundant_when_the_hook_settled_and_the_db_image_is_here(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    _db_present(monkeypatch, config, True)
    assert pre_up_build_is_redundant(config, "compose.yml", _env(), dry_run=False) is True


def test_not_redundant_without_the_db_image(config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The label check is satisfied by a matching running *container* without looking for the
    image, so a running-but-untagged db would otherwise send `docker run` to the registry for
    a tag only ever built locally — the `manifest unknown` shape from #61."""
    _db_present(monkeypatch, config, False)
    assert pre_up_build_is_redundant(config, "compose.yml", _env(), dry_run=False) is False


def test_never_redundant_on_a_dry_run(config, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dry run reports staleness without rebuilding, so it establishes nothing."""
    _db_present(monkeypatch, config, True)
    assert pre_up_build_is_redundant(config, "compose.yml", _env(), dry_run=True) is False


@pytest.mark.parametrize("env_add", [_env(sha=""), _env(tag=""), {}])
def test_not_redundant_when_there_was_nothing_to_compare(
    config, monkeypatch: pytest.MonkeyPatch, env_add: dict
) -> None:
    """`stack_images_match_checkout` answers `None` for "matches" *and* for "cannot compare"
    — the latter when `git rev-parse` fails. Only the first is a verification, so a failed
    rev-parse must keep building rather than read as a clean bill of health."""
    _db_present(monkeypatch, config, True)
    assert pre_up_build_is_redundant(config, "compose.yml", env_add, dry_run=False) is False


def test_a_dry_run_does_not_even_ask_docker(config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces on the ordering: `dry_run` short-circuits before any docker call, so a
    future change to `stack_db_image_present` cannot make a dry run do work."""

    def explode(*_a, **_k):
        raise AssertionError("dry run must not call docker")

    monkeypatch.setattr(stale_stack, "run_cmd", explode)
    assert pre_up_build_is_redundant(config, "compose.yml", _env(), dry_run=True) is False


# --- the pre-`up` build itself --------------------------------------------------------------


def _drive_compose_up(
    config, monkeypatch: pytest.MonkeyPatch, **kwargs
) -> list[str]:
    """Run `_run_compose_path` for an `up`, returning the side effects it performed."""
    done: list[str] = []

    class _Proc:
        returncode = 0

    monkeypatch.setattr(env_handlers, "should_materialize_for_compose", lambda _a: True)
    monkeypatch.setattr(
        env_handlers, "_ensure_stack_volumes", lambda *a, **k: done.append("volumes") or 0
    )
    monkeypatch.setattr(
        env_handlers,
        "_ensure_local_stack_images_built",
        lambda *a, **k: done.append("build") or 0,
    )
    monkeypatch.setattr(
        env_handlers, "materialize_configs", lambda *a, **k: done.append("configs") or 0
    )
    monkeypatch.setattr(
        env_handlers, "postgres_image_from_env", lambda *a, **k: "reg/db:x"
    )
    monkeypatch.setattr(env_handlers, "write_label_override", lambda *a, **k: None)
    monkeypatch.setattr(
        env_handlers, "local_proxy_extra_compose_files", lambda *a, **k: []
    )
    monkeypatch.setattr(
        env_handlers, "dc_backup_tls_extra_compose_files", lambda *a, **k: []
    )
    monkeypatch.setattr(
        env_handlers, "_compose", lambda *a, **k: done.append("up") or _Proc()
    )

    rc = env_handlers._run_compose_path(
        argparse.Namespace(dry_run=False, yes=True, tag=None),
        config,
        info={},
        compose_file="compose.yml",
        env_add={},
        env_name="dev",
        compose_args=["up", "-d"],
        use_prepulled_registry=False,
        site_origin="",
        docker_host="",
        storage_volumes={},
        **kwargs,
    )
    assert rc == 0
    return done


def test_the_pre_up_build_runs_by_default(config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The premise of the test below.

    Without this, a `_run_compose_path` that had simply lost the build call would pass it.
    It also pins the default for every caller that does not pass the new argument.
    """
    assert _drive_compose_up(config, monkeypatch) == ["volumes", "build", "configs", "up"]


def test_the_pre_up_build_is_skipped_once_the_hook_has_settled_the_images(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#75: the hook either verified the images or rebuilt them with the identical call."""
    done = _drive_compose_up(config, monkeypatch, stack_images_already_built=True)
    assert done == ["volumes", "configs", "up"]
    assert "build" not in done


def test_skipping_the_build_still_materializes_configs_and_ups(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build is the only thing that goes; volume init, configs and the `up` are untouched,
    and compose's own `--build` on that `up` is what still catches an edited Dockerfile."""
    done = _drive_compose_up(config, monkeypatch, stack_images_already_built=True)
    assert done[-1] == "up"
    assert "volumes" in done and "configs" in done
