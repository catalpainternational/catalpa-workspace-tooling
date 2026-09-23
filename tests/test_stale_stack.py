"""Stale local stack detection: image labels vs the current checkout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catalpa_tooling import stale_stack
from catalpa_tooling.config import load_project_config
from catalpa_tooling.stale_stack import (
    LABEL_GIT_SHA,
    LABEL_IMAGE_TAG,
    SKIP_ENV_VAR,
    drop_no_build_when_stale,
    label_override_yaml,
    should_ensure_stack_checkout,
    stack_images_match_checkout,
    write_label_override,
)
from tests.helpers import write_minimal_tooling_tree

TAG = "my-branch"
SHA = "abc1234"


@pytest.fixture
def config(tmp_path: Path):
    write_minimal_tooling_tree(tmp_path)
    return load_project_config(tmp_path)


def _env(tag: str = TAG, sha: str = SHA) -> dict[str, str]:
    return {"STACK_IMAGE_TAG": tag, "VITE_GIT_SHA": sha}


def _fake_docker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    services: dict,
    running: dict[str, str] | None = None,
    labels: dict[str, dict | None],
) -> list[list[str]]:
    """Fake every docker call stale_stack makes; returns the captured argv list.

    ``labels`` maps a container id or image ref to its labels dict, or None when docker has no
    such object locally.
    """
    calls: list[list[str]] = []
    running = running or {}

    class _Proc:
        def __init__(self, returncode: int = 0, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["docker", "compose", "-f"] and "config" in cmd:
            return _Proc(stdout=json.dumps({"services": services}))
        if cmd[:3] == ["docker", "compose", "-f"] and "ps" in cmd:
            service = cmd[-1]
            return _Proc(stdout=running.get(service, ""))
        if cmd[:2] == ["docker", "container"] or cmd[:2] == ["docker", "image"]:
            ref = cmd[3]
            if ref not in labels or labels[ref] is None:
                return _Proc(returncode=1)
            return _Proc(stdout=json.dumps(labels[ref]))
        raise AssertionError(f"unexpected docker call: {cmd}")

    monkeypatch.setattr(stale_stack, "run_cmd", fake_run)
    return calls


def _build_service(image: str) -> dict:
    return {"image": image, "build": {"context": "."}}


def _matching_labels() -> dict[str, str]:
    return {LABEL_GIT_SHA: SHA, LABEL_IMAGE_TAG: TAG}


# --- should_ensure_stack_checkout -------------------------------------------------------


@pytest.mark.parametrize(
    "env_command,compose_args,expected",
    [
        ("db", None, True),
        ("manage", None, True),
        ("files", None, True),
        (None, [], True),
        (None, ["up", "-d"], True),
        (None, ["ps"], False),
        (None, ["logs", "-f"], False),
        (None, ["down"], False),
        (None, ["config"], False),
        ("compose", ["down", "-v"], False),
        ("wipe", [], False),
        ("info", None, True),  # returns before the hook; never actually reached
        ("docker", None, False),
        ("zabbix", None, False),
    ],
)
def test_should_ensure_stack_checkout(env_command, compose_args, expected) -> None:
    assert (
        should_ensure_stack_checkout(
            env_command, compose_args, use_prepulled_registry=False
        )
        is expected
    )


def test_prepulled_registry_skips_the_check() -> None:
    """Pinned remote envs are unchanged by this feature."""
    assert not should_ensure_stack_checkout("db", None, use_prepulled_registry=True)


def test_skip_env_var_opts_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SKIP_ENV_VAR, "1")
    assert not should_ensure_stack_checkout("db", None, use_prepulled_registry=False)


def test_skip_env_var_only_honours_exactly_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SKIP_ENV_VAR, "0")
    assert should_ensure_stack_checkout("db", None, use_prepulled_registry=False)


# --- label override ---------------------------------------------------------------------


def test_label_override_covers_every_stack_build_service(config) -> None:
    """Service names are per-project, so the override is generated rather than shipped."""
    text = label_override_yaml(config)
    for service in ("db", "web", "proxy"):
        assert f"  {config.stack_service(service)}:" in text
    assert text.count("build:") == 3
    assert f'{LABEL_GIT_SHA}: "${{VITE_GIT_SHA}}"' in text
    assert f'{LABEL_IMAGE_TAG}: "${{STACK_IMAGE_TAG}}"' in text


def test_write_label_override_is_outside_the_repo(config, tmp_path: Path) -> None:
    """Nothing to commit and nothing to gitignore, so projects need no repo changes."""
    path = Path(write_label_override(config))
    try:
        assert path.is_file()
        assert tmp_path not in path.parents
        assert "services:" in path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)


# --- detection --------------------------------------------------------------------------


def test_matching_labels_are_not_stale(config, monkeypatch: pytest.MonkeyPatch) -> None:
    services = {
        config.stack_service(r): _build_service(f"reg/{r}:{TAG}")
        for r in ("db", "web", "proxy")
    }
    _fake_docker(
        monkeypatch,
        services=services,
        labels={f"reg/{r}:{TAG}": _matching_labels() for r in ("db", "web", "proxy")},
    )
    assert stack_images_match_checkout(config, "compose.yml", _env()) is None


def test_tag_mismatch_is_stale(config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The branch-switch case: containers still running last branch's images."""
    db = config.stack_service("db")
    _fake_docker(
        monkeypatch,
        services={db: _build_service(f"reg/db:{TAG}")},
        running={db: "container1"},
        labels={"container1": {LABEL_GIT_SHA: SHA, LABEL_IMAGE_TAG: "other-branch"}},
    )
    reason = stack_images_match_checkout(config, "compose.yml", _env())
    assert reason is not None
    assert reason.containers_running
    assert "other-branch" in reason.detail


def test_sha_mismatch_is_stale(config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same branch, HEAD moved — the tag alone cannot catch this."""
    db = config.stack_service("db")
    _fake_docker(
        monkeypatch,
        services={db: _build_service(f"reg/db:{TAG}")},
        labels={f"reg/db:{TAG}": {LABEL_GIT_SHA: "0000000", LABEL_IMAGE_TAG: TAG}},
    )
    reason = stack_images_match_checkout(config, "compose.yml", _env())
    assert reason is not None
    assert "0000000" in reason.detail


def test_missing_labels_are_stale(config, monkeypatch: pytest.MonkeyPatch) -> None:
    """First command after upgrading: images predate the labels, so rebuild once."""
    db = config.stack_service("db")
    _fake_docker(
        monkeypatch,
        services={db: _build_service(f"reg/db:{TAG}")},
        labels={f"reg/db:{TAG}": {"com.docker.compose.project": "x"}},
    )
    reason = stack_images_match_checkout(config, "compose.yml", _env())
    assert reason is not None
    assert "no catalpa.* build labels" in reason.detail


def test_no_container_and_no_local_image_is_stale(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The NCD repro: stack down, branch tag never built.

    The issue's original rule 4 called this "not stale", which is exactly what let
    `dk dev db restore` proceed to `manifest unknown`. Nothing to compare against means build.
    """
    db = config.stack_service("db")
    _fake_docker(
        monkeypatch,
        services={db: _build_service(f"reg/db:{TAG}")},
        labels={},
    )
    reason = stack_images_match_checkout(config, "compose.yml", _env())
    assert reason is not None
    assert not reason.containers_running
    assert "no running container and no local image" in reason.detail


def test_image_only_service_is_never_stale(config, monkeypatch: pytest.MonkeyPatch) -> None:
    """A service with no `build:` can never carry the labels.

    Reporting it stale would rebuild on every single command, forever, with no way to converge.
    """
    db = config.stack_service("db")
    _fake_docker(
        monkeypatch,
        services={db: {"image": "postgres:17"}},  # no build section
        labels={},
    )
    assert stack_images_match_checkout(config, "compose.yml", _env()) is None


def test_missing_expected_values_skips_the_check(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed `git rev-parse` must not make everything look stale."""
    calls = _fake_docker(monkeypatch, services={}, labels={})
    assert stack_images_match_checkout(config, "compose.yml", _env(sha="")) is None
    assert stack_images_match_checkout(config, "compose.yml", _env(tag="")) is None
    assert calls == []  # no inspect tax when there is nothing to compare


# --- rebuild behaviour ------------------------------------------------------------------


def _patch_rebuild(monkeypatch: pytest.MonkeyPatch, reason) -> list[str]:
    actions: list[str] = []
    monkeypatch.setattr(
        stale_stack, "stack_images_match_checkout", lambda *_a, **_k: reason
    )
    import catalpa_tooling.compose as compose_mod
    import catalpa_tooling.remote_deploy as remote_deploy_mod

    monkeypatch.setattr(
        remote_deploy_mod,
        "_ensure_local_stack_images_built",
        lambda *_a, **_k: actions.append("build") or 0,
    )

    class _Proc:
        returncode = 0

    def fake_compose(_compose_file, *args, **_kwargs):
        actions.append(f"compose {' '.join(args)}")
        return _Proc()

    monkeypatch.setattr(compose_mod, "_compose", fake_compose)
    return actions


def test_stopped_stack_builds_only(config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not surprise-start a stack the user deliberately left down."""
    reason = stale_stack.StaleReason("db", "no local image", containers_running=False)
    actions = _patch_rebuild(monkeypatch, reason)
    rc, got = stale_stack.ensure_stack_matches_checkout(
        config, "compose.yml", _env(), use_prepulled_registry=False
    )
    assert rc == 0
    assert got is reason
    assert actions == ["build"]


def test_running_stack_is_recreated(config, monkeypatch: pytest.MonkeyPatch) -> None:
    reason = stale_stack.StaleReason("db", "tag drift", containers_running=True)
    actions = _patch_rebuild(monkeypatch, reason)
    rc, _ = stale_stack.ensure_stack_matches_checkout(
        config, "compose.yml", _env(), use_prepulled_registry=False
    )
    assert rc == 0
    assert actions == ["build", "compose up -d --build"]


def test_recreate_false_leaves_the_up_to_the_caller(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dk dev up` runs its own `up` moments later; recreating twice is pure waste."""
    reason = stale_stack.StaleReason("db", "tag drift", containers_running=True)
    actions = _patch_rebuild(monkeypatch, reason)
    rc, _ = stale_stack.ensure_stack_matches_checkout(
        config, "compose.yml", _env(), use_prepulled_registry=False, recreate=False
    )
    assert rc == 0
    assert actions == ["build"]


def test_failed_build_aborts(config, monkeypatch: pytest.MonkeyPatch) -> None:
    reason = stale_stack.StaleReason("db", "tag drift", containers_running=True)
    _patch_rebuild(monkeypatch, reason)
    import catalpa_tooling.remote_deploy as remote_deploy_mod

    monkeypatch.setattr(
        remote_deploy_mod, "_ensure_local_stack_images_built", lambda *_a, **_k: 3
    )
    rc, _ = stale_stack.ensure_stack_matches_checkout(
        config, "compose.yml", _env(), use_prepulled_registry=False
    )
    assert rc == 3


def test_dry_run_reports_without_rebuilding(
    config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A central hook that can burn fifteen minutes on a dry run is its own footgun."""
    reason = stale_stack.StaleReason("db", "tag drift", containers_running=True)
    actions = _patch_rebuild(monkeypatch, reason)
    rc, _ = stale_stack.ensure_stack_matches_checkout(
        config, "compose.yml", _env(), use_prepulled_registry=False, dry_run=True
    )
    assert rc == 0
    assert actions == []
    assert "would rebuild" in capsys.readouterr().err


def test_not_stale_does_nothing(config, monkeypatch: pytest.MonkeyPatch) -> None:
    actions = _patch_rebuild(monkeypatch, None)
    rc, got = stale_stack.ensure_stack_matches_checkout(
        config, "compose.yml", _env(), use_prepulled_registry=False
    )
    assert (rc, got) == (0, None)
    assert actions == []


# --- the override actually reaches the build ---------------------------------------------


def test_compose_yml_build_passes_the_label_override(
    config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this the images never get labelled and every command would look stale."""
    import catalpa_tooling.dk_stack as dk_stack

    seen: dict[str, object] = {}

    class _Proc:
        returncode = 0

    def fake_run(cmd, **_kwargs):
        seen["cmd"] = list(cmd)
        # The file must still exist while compose runs, and be cleaned up afterwards.
        override = cmd[cmd.index("-f", 4) + 1]
        seen["override_text"] = Path(override).read_text(encoding="utf-8")
        seen["override_path"] = override
        return _Proc()

    monkeypatch.setattr(dk_stack, "run_cmd", fake_run)
    monkeypatch.setattr(dk_stack, "restore_controlling_tty", lambda: None)

    assert dk_stack.compose_yml_build(config, env_add={}) == 0

    cmd = seen["cmd"]
    assert cmd[:2] == ["docker", "compose"]
    assert cmd.count("-f") == 2
    assert LABEL_GIT_SHA in str(seen["override_text"])
    assert not Path(str(seen["override_path"])).exists()  # temp file cleaned up


def test_compose_path_stamps_labels_only_when_it_may_build() -> None:
    from catalpa_tooling.env_handlers import _compose_args_may_build

    assert _compose_args_may_build([]) is True  # defaults to `up -d`
    assert _compose_args_may_build(["up", "-d"]) is True
    assert _compose_args_may_build(["up", "-d", "--build"]) is True
    assert _compose_args_may_build(["build"]) is True
    assert _compose_args_may_build(["up", "--no-build"]) is False
    assert _compose_args_may_build(["ps"]) is False
    assert _compose_args_may_build(["down"]) is False


# --- up --no-build ----------------------------------------------------------------------


def test_no_build_is_dropped_when_stale() -> None:
    assert drop_no_build_when_stale(["up", "-d", "--no-build"]) == ["up", "-d"]


def test_no_build_untouched_for_other_verbs() -> None:
    assert drop_no_build_when_stale(["down"]) == ["down"]
    assert drop_no_build_when_stale(["up", "-d"]) == ["up", "-d"]
    assert drop_no_build_when_stale([]) == []
