"""Tests for worktree overlays and ``dk worktree`` helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from catalpa_tooling.cli.dk_argv import peel_worktree_flag
from catalpa_tooling.config import ProjectConfig as ProjectConfigT, load_project_config
from catalpa_tooling.dk_cli import _load_config_for_argv
from catalpa_tooling.managed_deploy_env import load_managed_deploy_context
from catalpa_tooling.media_storage import MediaStorage, MediaStorageKind
from catalpa_tooling.worktree import (
    AGENTS_LOCAL_NAME,
    ensure_worktree_gitignore,
    media_dir_for_config,
    resolve_worktree_root,
    worktree_context,
    worktree_create,
    worktree_list,
    worktree_remove,
    worktree_seed,
    worktree_stack_status,
    worktree_status,
)
from catalpa_tooling.worktree_overlay import (
    WorktreeOverlayError,
    apply_worktree_overlay_to_info,
    build_worktree_overlay,
    force_worktree_origin_env,
    load_worktree_overlay,
    sanitize_worktree_slug,
    write_worktree_overlay,
)
from tests.helpers import git_init_commit, write_dev_env, write_minimal_tooling_tree


def _patch_media_transfer(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict],
    *,
    source_has_content: bool | None = True,
    transfer_rc: int = 0,
) -> tuple[MediaStorage, MediaStorage]:
    """Stand in for the volume machinery: resolve, probe and transfer.

    Returns the ``(source, destination)`` storages the patched resolver hands out. The
    destination carries the worktree's compose project, which is what the copy has to reach.
    """
    src_media = MediaStorage(MediaStorageKind.VOLUME, "app_compose_dev_django_media")
    dst_media = MediaStorage(MediaStorageKind.VOLUME, "app_compose_dev_feat_a_django_media")

    def fake_resolve(*, compose_file, env, config, repo_root=None):
        project = (env.get("COMPOSE_PROJECT_NAME") or "").strip()
        return dst_media if project.endswith("_feat_a") else src_media

    monkeypatch.setattr("catalpa_tooling.worktree.resolve_media_storage", fake_resolve)
    monkeypatch.setattr(
        "catalpa_tooling.worktree.media_storage_has_content",
        lambda storage, env, **k: source_has_content,
    )
    monkeypatch.setattr(
        "catalpa_tooling.worktree._ensure_destination_media_volume",
        lambda *a, **k: None,
    )

    def fake_transfer(src_env, dst_env, **kwargs):
        calls.append({"src_env": src_env, "dst_env": dst_env, **kwargs})
        return transfer_rc

    monkeypatch.setattr("catalpa_tooling.worktree.run_transfer_media", fake_transfer)
    return src_media, dst_media


def test_sanitize_worktree_slug() -> None:
    assert sanitize_worktree_slug("My-Feature") == "my_feature"
    assert sanitize_worktree_slug("foo_bar") == "foo_bar"
    with pytest.raises(WorktreeOverlayError):
        sanitize_worktree_slug("!!!")
    with pytest.raises(WorktreeOverlayError):
        sanitize_worktree_slug("123abc")


def test_peel_worktree_flag() -> None:
    slug, rest = peel_worktree_flag(["--worktree", "onboarding", "dev", "up", "-d"])
    assert slug == "onboarding"
    assert rest == ["dev", "up", "-d"]
    slug, rest = peel_worktree_flag(["-W", "foo", "worktree", "list"])
    assert slug == "foo" and rest == ["worktree", "list"]
    slug, rest = peel_worktree_flag(["--worktree=bar", "dev", "ps"])
    assert slug == "bar" and rest == ["dev", "ps"]
    slug, rest = peel_worktree_flag(["dev", "up"])
    assert slug is None and rest == ["dev", "up"]
    with pytest.raises(ValueError, match="requires a worktree slug"):
        peel_worktree_flag(["--worktree"])
    with pytest.raises(ValueError, match="only be given once"):
        peel_worktree_flag(["-W", "a", "--worktree", "b", "dev", "up"])


def test_build_and_apply_overlay(tmp_path: Path, isolated_tooling: None) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    config = load_project_config(tmp_path)
    overlay = build_worktree_overlay(
        config, slug="foo", base_env="dev", parent_repo_root=tmp_path
    )
    assert overlay.compose_project_name == "app_compose_dev_foo"
    assert overlay.site_origin == "https://minimal-dev-foo.localdev.temp.build"

    info = yaml.safe_load((tmp_path / "docker" / "envs" / "dev" / "info.yaml").read_text())
    remapped = apply_worktree_overlay_to_info(info, overlay, env_name="dev")
    assert remapped is not None
    assert remapped["site_origin"] == overlay.site_origin
    assert remapped["env"]["compose_project_name"] == overlay.compose_project_name
    assert "bero_origin" not in remapped["env"]

    info_remote = {**info, "docker_host": "ssh://user@host"}
    assert apply_worktree_overlay_to_info(info_remote, overlay, env_name="dev") is None
    assert apply_worktree_overlay_to_info(info, overlay, env_name="full") is None


def test_force_worktree_origin_env() -> None:
    from catalpa_tooling.worktree_overlay import WorktreeOverlay

    ov = WorktreeOverlay(
        version=1,
        slug="foo",
        base_env="dev",
        compose_project_name="app_compose_dev_foo",
        site_origin="https://minimal-dev-foo.localdev.temp.build",
    )
    env_add: dict[str, str] = {
        "COMPOSE_PROJECT_NAME": "app_compose_dev",
        "SITE_ORIGIN": "https://minimal-dev.localdev.temp.build",
        "DJANGO_ORIGIN": "https://admin.minimal-dev.localdev.temp.build",
    }
    info = {"local_proxy": {"roles": ["admin", "stats"]}}
    force_worktree_origin_env(env_add, overlay=ov, info=info)
    assert env_add["COMPOSE_PROJECT_NAME"] == "app_compose_dev_foo"
    assert env_add["SITE_ORIGIN"] == ov.site_origin
    assert env_add["DJANGO_ORIGIN"] == "https://admin.minimal-dev-foo.localdev.temp.build"
    assert env_add["METABASE_ORIGIN"] == "https://stats.minimal-dev-foo.localdev.temp.build"
    assert env_add["CADDY_SITE_ADDRESS"] == "http://minimal-dev-foo.localdev.temp.build"


def test_load_managed_deploy_applies_overlay(
    tmp_path: Path, isolated_tooling: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    config = load_project_config(tmp_path)
    overlay = build_worktree_overlay(config, slug="bar", parent_repo_root=tmp_path)
    write_worktree_overlay(tmp_path, overlay)

    monkeypatch.setattr(
        "catalpa_tooling.ssh_known_hosts.ensure_ssh_known_host_for_docker_host",
        lambda *a, **k: 0,
    )

    ctx = load_managed_deploy_context(config, "dev")
    assert ctx is not None
    assert ctx.site_origin == overlay.site_origin
    assert ctx.info["site_origin"] == overlay.site_origin
    assert ctx.info["env"]["compose_project_name"] == overlay.compose_project_name
    assert ctx.env_add["COMPOSE_PROJECT_NAME"] == overlay.compose_project_name
    assert ctx.env_add["BERO_ORIGIN"] == overlay.site_origin
    assert ctx.env_add["DJANGO_EXTRA_ORIGINS"] == (
        "https://admin.minimal-dev-bar.localdev.temp.build, "
        "https://stats.minimal-dev-bar.localdev.temp.build"
    )

    from catalpa_tooling.local_proxy import local_proxy_routes, route_id_for_host

    routes = local_proxy_routes(
        ctx.info, config, "dev", ctx.env_add["COMPOSE_PROJECT_NAME"]
    )
    hosts = {r.host for r in routes}
    assert "minimal-dev-bar.localdev.temp.build" in hosts
    assert "admin.minimal-dev-bar.localdev.temp.build" in hosts
    assert "stats.minimal-dev-bar.localdev.temp.build" in hosts
    assert "minimal-dev.localdev.temp.build" not in hosts
    assert all(
        r.upstream_dial.startswith("app_compose_dev_bar-") for r in routes
    )
    main_route_id = route_id_for_host(
        config, "dev", "minimal-dev.localdev.temp.build"
    )
    assert all(r.route_id != main_route_id for r in routes)

    ctx_plain = load_managed_deploy_context(config, "dev", apply_worktree=False)
    assert ctx_plain is not None
    assert ctx_plain.site_origin == "https://minimal-dev.localdev.temp.build"
    assert ctx_plain.info["site_origin"] == "https://minimal-dev.localdev.temp.build"
    assert ctx_plain.env_add.get("COMPOSE_PROJECT_NAME") == "app_compose_dev"


def test_ensure_worktree_gitignore(tmp_path: Path) -> None:
    missing = ensure_worktree_gitignore(tmp_path)
    assert ".worktrees/" in missing
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".worktrees/" in text
    assert ".catalpa-worktree.yaml" in text
    assert AGENTS_LOCAL_NAME in text
    assert ensure_worktree_gitignore(tmp_path) == []


def test_worktree_create_list_remove_git(
    tmp_path: Path, isolated_tooling: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `remove` now stops the stack, so without this the test would shell out to a real
    # `docker compose down` and depend on a running daemon.
    monkeypatch.setattr(
        "catalpa_tooling.worktree._compose",
        lambda *a, **k: subprocess.CompletedProcess(list(a), 0),
    )
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)

    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="feat-a", dry_run=False, seed=False) == 0
    wt = tmp_path / ".worktrees" / "feat_a"
    assert wt.is_dir()
    assert load_worktree_overlay(wt) is not None
    assert ".worktrees/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")

    assert worktree_list(config) == 0

    wt_config = load_project_config(wt)
    assert media_dir_for_config(wt_config) == wt / "media"

    # Media is a stack-to-stack copy now; the storages and the transfer are covered in
    # their own tests below, so here just confirm the step wiring runs end to end.
    src_media, dst_media = _patch_media_transfer(monkeypatch, calls := [])
    assert worktree_seed(
        wt_config, do_db=False, do_media=True, dry_run=True, yes=True
    ) == 0
    assert calls == []
    assert worktree_seed(
        wt_config, do_db=False, do_media=True, dry_run=False, yes=True
    ) == 0
    assert len(calls) == 1
    assert calls[0]["src_media"] == src_media
    assert calls[0]["dst_media"] == dst_media

    calls.clear()
    assert worktree_seed(
        config, slug="feat_a", do_db=False, do_media=True, dry_run=False, yes=True
    ) == 0
    assert len(calls) == 1

    from catalpa_tooling.worktree import worktree_remove

    assert worktree_remove(config, slug="feat-a", yes=True) == 0
    assert not wt.exists()


def test_worktree_create_inits_submodules(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    (tmp_path / ".gitmodules").write_text(
        '[submodule "bero"]\n\tpath = bero\n\turl = https://example.com/bero.git\n',
        encoding="utf-8",
    )
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)

    calls: list[tuple[str, ...]] = []

    def fake_git(
        *args: str,
        cwd: Path,
        check: bool = True,
        capture: bool = False,
        dry_run: bool = False,
    ):
        calls.append(args)
        if args[:2] == ("worktree", "add"):
            # worktree add -b BRANCH DEST START
            dest = Path(args[4] if args[2] == "-b" else args[2])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".gitmodules").write_text(
                (tmp_path / ".gitmodules").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (dest / "tooling.yaml").write_text(
                (tmp_path / "tooling.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")

    monkeypatch.setattr("catalpa_tooling.worktree._git", fake_git)
    monkeypatch.setattr("catalpa_tooling.worktree._branch_exists", lambda *a, **k: False)

    # No local bero → remote fetch (no --reference)
    assert worktree_create(config, slug="with-sub", dry_run=False, seed=False) == 0
    remote_calls = [
        c
        for c in calls
        if c[:3] == ("submodule", "update", "--init")
        and "--recursive" in c
        and "--depth" in c
        and "1" in c
    ]
    assert remote_calls
    assert all("--reference" not in c for c in remote_calls)
    assert any(c[-2:] == ("--", "bero") for c in remote_calls)

    # Local bero present → --reference main/bero
    bero = tmp_path / "bero"
    bero.mkdir()
    (bero / ".git").mkdir()

    calls.clear()
    assert worktree_create(config, slug="with-ref", dry_run=False, seed=False) == 0
    ref_calls = [c for c in calls if c[:3] == ("submodule", "update", "--init")]
    assert ref_calls
    assert any(
        "--reference" in c and str(bero.resolve()) in c and c[-2:] == ("--", "bero")
        for c in ref_calls
    )

    calls.clear()
    assert worktree_create(
        config, slug="full-sub", dry_run=False, shallow_submodules=False, seed=False
    ) == 0
    assert any(
        c[:3] == ("submodule", "update", "--init")
        and "--recursive" in c
        and "--depth" not in c
        and "--reference" in c
        for c in calls
    )

    calls.clear()
    assert worktree_create(config, slug="no-sub", dry_run=False, init_submodules=False, seed=False) == 0
    assert not any(c[:2] == ("submodule", "update") for c in calls)


def test_worktree_create_dry_run_mentions_submodules(
    tmp_path: Path, isolated_tooling: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    (tmp_path / ".gitmodules").write_text('[submodule "bero"]\npath = bero\n', encoding="utf-8")
    bero = tmp_path / "bero"
    bero.mkdir()
    (bero / ".git").mkdir()
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="dry", dry_run=True, seed=False) == 0
    err = capsys.readouterr().err
    assert "submodule update --init --recursive --depth 1" in err
    assert f"--reference {bero.resolve()}" in err
    assert "-- bero" in err
    assert not (tmp_path / ".worktrees" / "dry").exists()


def test_worktree_create_seeds_by_default(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)

    seed_calls: list[dict] = []

    def fake_seed(*args, **kwargs):
        seed_calls.append(kwargs)
        return 0

    monkeypatch.setattr("catalpa_tooling.worktree.worktree_seed", fake_seed)
    monkeypatch.setattr(
        "catalpa_tooling.worktree._git",
        lambda *a, cwd, check=True, capture=False, dry_run=False: (
            (Path(a[4] if a[2] == "-b" else a[2]).mkdir(parents=True, exist_ok=True)
            or subprocess.CompletedProcess(list(a), 0, stdout="", stderr=""))
            if a[:2] == ("worktree", "add")
            else subprocess.CompletedProcess(list(a), 0, stdout="", stderr="")
        ),
    )
    monkeypatch.setattr("catalpa_tooling.worktree._branch_exists", lambda *a, **k: False)

    assert worktree_create(config, slug="seeded", dry_run=False, init_submodules=False) == 0
    assert len(seed_calls) == 1
    assert seed_calls[0]["slug"] == "seeded"
    assert seed_calls[0]["yes"] is True
    assert seed_calls[0]["do_db"] is True
    assert seed_calls[0]["do_media"] is True

    seed_calls.clear()
    assert worktree_create(config, slug="no-seed", dry_run=False, init_submodules=False, seed=False) == 0
    assert seed_calls == []


def test_worktree_create_dry_run_prints_seed(
    tmp_path: Path, isolated_tooling: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="dry-seed", dry_run=True, init_submodules=False) == 0
    err = capsys.readouterr().err
    assert "dry-run: seed DB + media from main dev → worktree 'dry_seed'" in err


def test_resolve_worktree_root_and_retarget(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="onboarding", dry_run=False, seed=False) == 0
    wt = resolve_worktree_root(tmp_path, "onboarding")
    assert wt == tmp_path / ".worktrees" / "onboarding"
    with pytest.raises(WorktreeOverlayError, match="not found"):
        resolve_worktree_root(tmp_path, "missing")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "catalpa_tooling.ssh_known_hosts.ensure_ssh_known_host_for_docker_host",
        lambda *a, **k: 0,
    )
    cfg, rest, slug = _load_config_for_argv(["--worktree", "onboarding", "dev", "ps"])
    assert slug == "onboarding"
    assert rest == ["dev", "ps"]
    assert Path.cwd() == wt.resolve()
    assert cfg.repo_root == wt.resolve()
    ctx = load_managed_deploy_context(cfg, "dev")
    assert ctx is not None
    assert ctx.env_add["COMPOSE_PROJECT_NAME"] == "app_compose_dev_onboarding"

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        _load_config_for_argv(["-W", "onboarding", "worktree", "create", "x"])
    assert exc.value.code == 2


def test_worktree_create_writes_agents_local(
    tmp_path: Path, isolated_tooling: None
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="agents", dry_run=False, seed=False) == 0
    wt = tmp_path / ".worktrees" / "agents"
    agents_path = wt / AGENTS_LOCAL_NAME
    assert agents_path.is_file()
    text = agents_path.read_text(encoding="utf-8")
    assert "agents" in text
    assert "app_compose_dev_agents" in text
    assert str(wt.resolve()) in text


def test_worktree_create_bring_up_calls_worktree_up(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)

    up_calls: list[dict] = []

    def fake_up(*args, **kwargs):
        up_calls.append(kwargs)
        return 0

    monkeypatch.setattr("catalpa_tooling.worktree.worktree_up", fake_up)
    monkeypatch.setattr(
        "catalpa_tooling.worktree._git",
        lambda *a, cwd, check=True, capture=False, dry_run=False: (
            (Path(a[4] if a[2] == "-b" else a[2]).mkdir(parents=True, exist_ok=True)
            or subprocess.CompletedProcess(list(a), 0, stdout="", stderr=""))
            if a[:2] == ("worktree", "add")
            else subprocess.CompletedProcess(list(a), 0, stdout="", stderr="")
        ),
    )
    monkeypatch.setattr("catalpa_tooling.worktree._branch_exists", lambda *a, **k: False)
    monkeypatch.setattr("catalpa_tooling.worktree.worktree_seed", lambda *a, **k: 0)

    assert worktree_create(
        config, slug="up-me", dry_run=False, init_submodules=False, bring_up=True
    ) == 0
    assert len(up_calls) == 1
    assert up_calls[0]["slug"] == "up_me"


def test_worktree_context_json_shape(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="ctx", dry_run=False, seed=False) == 0

    monkeypatch.setattr(
        "catalpa_tooling.worktree.worktree_stack_status",
        lambda *a, **k: "running",
    )
    monkeypatch.setattr(
        "catalpa_tooling.worktree._media_seeded",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "catalpa_tooling.worktree._git_head_info",
        lambda *a, **k: ("worktree/ctx", "abc1234"),
    )

    capsys.readouterr()
    assert worktree_context(config, slug="ctx", as_json=True) == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["slug"] == "ctx"
    assert payload["status"] == "running"
    assert payload["seeded"] is True
    assert payload["compose_project_name"] == "app_compose_dev_ctx"
    assert payload["base_env"] == "dev"
    assert payload["base_ref"]
    assert payload["base_commit"]
    assert "head_commit" in payload
    assert "worktree" in payload


def test_list_and_status_tolerate_docker_failure(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="nodocker", dry_run=False, seed=False) == 0

    def boom(*args, **kwargs):
        raise OSError("docker unavailable")

    monkeypatch.setattr("catalpa_tooling.worktree._compose", boom)
    wt = tmp_path / ".worktrees" / "nodocker"
    wt_config = load_project_config(wt)
    overlay = load_worktree_overlay(wt)
    assert overlay is not None
    assert worktree_stack_status(wt_config, overlay) == "unknown"

    assert worktree_list(config) == 0
    out = capsys.readouterr().out
    assert "stack=unknown" in out

    capsys.readouterr()
    assert worktree_status(config, slug="nodocker") == 0
    assert "stack: unknown" in capsys.readouterr().out


def test_worktree_cli_subcommands_registered() -> None:
    import argparse

    from catalpa_tooling.worktree_cli import attach_worktree_subcommands

    parser = argparse.ArgumentParser()
    attach_worktree_subcommands(parser)
    for name in ("up", "down", "restart", "logs", "status", "context"):
        with pytest.raises(SystemExit):
            parser.parse_args([name, "--help"])


def test_cmd_worktree_dispatches_up(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    from catalpa_tooling.worktree_cli import cmd_worktree

    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    config = load_project_config(tmp_path)

    called: list[str] = []

    def fake_up(config, *, slug=None, dry_run=False):
        called.append(slug or "")
        return 0

    monkeypatch.setattr("catalpa_tooling.worktree_cli.worktree_up", fake_up)
    ns = argparse.Namespace(worktree_command="up", slug="feat", dry_run=False)
    assert cmd_worktree(ns, config) == 0
    assert called == ["feat"]


# --- issue #64: `worktree remove --wipe` must not leave the external: volumes behind ----------


def _wipe_recording_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A worktree plus recorders for the compose call and the volume removals."""
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="ds_180", dry_run=False, seed=False) == 0

    compose_calls: list[tuple] = []

    def fake_compose(*args, **kwargs):
        compose_calls.append(args)
        return subprocess.CompletedProcess(list(args), 0)

    removed: list[str] = []

    def fake_run(cmd, **kwargs):
        if list(cmd[:3]) == ["docker", "volume", "rm"]:
            removed.append(cmd[3])
            return subprocess.CompletedProcess(list(cmd), 0, stdout="", stderr="")
        return subprocess.CompletedProcess(list(cmd), 0, stdout="", stderr="")

    monkeypatch.setattr("catalpa_tooling.worktree._compose", fake_compose)
    monkeypatch.setattr("catalpa_tooling.pgbackrest_volume_config.run_cmd", fake_run)
    return config, compose_calls, removed


def test_worktree_remove_wipe_removes_external_volumes(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#64: `compose down -v` alone left PGDATA and the conf volumes orphaned."""
    config, compose_calls, removed = _wipe_recording_repo(tmp_path, monkeypatch)

    assert worktree_remove(config, slug="ds_180", wipe=True, yes=True) == 0

    assert compose_calls, "compose down -v must still run first"
    assert "down" in compose_calls[0] and "-v" in compose_calls[0]

    # Every external: volume for the retired project name, PGDATA included.
    assert removed, "no docker volume rm issued — the external volumes would survive"
    for expected in ("postgres_data", "django_media", "caddy_data", "postgres_conf", "pgbackrest_conf"):
        assert any(name.endswith(expected) for name in removed), f"{expected} left behind: {removed}"


def test_worktree_remove_wipe_attempts_every_volume_and_keeps_the_checkout(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """One stuck volume must not hide the rest, nor silently strand them.

    The checkout is kept on purpose so the command can simply be re-run; removing it would
    leave orphans nobody goes looking for, which is the failure #64 is about.
    """
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="ds_180", dry_run=False, seed=False) == 0

    attempted: list[str] = []

    def fake_run(cmd, **kwargs):
        if list(cmd[:3]) == ["docker", "volume", "rm"]:
            attempted.append(cmd[3])
            if cmd[3].endswith("postgres_data"):  # the first one, and it is stuck
                return subprocess.CompletedProcess(
                    list(cmd), 1, stdout="", stderr="volume is in use - [abc123]"
                )
        return subprocess.CompletedProcess(list(cmd), 0, stdout="", stderr="")

    monkeypatch.setattr(
        "catalpa_tooling.worktree._compose",
        lambda *a, **k: subprocess.CompletedProcess(list(a), 0),
    )
    monkeypatch.setattr("catalpa_tooling.pgbackrest_volume_config.run_cmd", fake_run)

    wt = tmp_path / ".worktrees" / "ds_180"
    assert worktree_remove(config, slug="ds_180", wipe=True, yes=True) != 0

    for expected in ("postgres_data", "django_media", "caddy_data", "postgres_conf", "pgbackrest_conf"):
        assert any(n.endswith(expected) for n in attempted), f"{expected} never attempted: {attempted}"
    assert wt.is_dir(), "checkout must survive so the wipe can be re-run"
    err = capsys.readouterr().err
    assert "could not be removed" in err
    assert "re-run" in err


def test_worktree_remove_wipe_dry_run_removes_nothing(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _, removed = _wipe_recording_repo(tmp_path, monkeypatch)
    assert worktree_remove(config, slug="ds_180", wipe=True, yes=True, dry_run=True) == 0
    assert removed == []


def test_worktree_remove_dry_run_does_not_claim_it_removed_anything(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """#108: `--dry-run` is read as a preview of a real `--wipe`; it must not report a removal."""
    config, _, _ = _wipe_recording_repo(tmp_path, monkeypatch)
    assert worktree_remove(config, slug="ds_180", wipe=True, yes=True, dry_run=True) == 0
    err = capsys.readouterr().err
    assert "Would remove worktree" in err
    assert "Removed worktree" not in err


def test_worktree_remove_without_dry_run_still_reports_the_removal(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    config, _, _ = _wipe_recording_repo(tmp_path, monkeypatch)
    assert worktree_remove(config, slug="ds_180", wipe=True, yes=True) == 0
    err = capsys.readouterr().err
    assert "Removed worktree" in err
    assert "Would remove worktree" not in err


# --- `remove` without --wipe must still stop the stack ----------------------------------------


def test_worktree_remove_stops_the_stack_but_keeps_volumes(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Retiring the checkout must not leave containers running for a project nothing can name.

    Volumes are a different question: the user did not ask for the data to go, so it stays —
    but the command says so, and says how to reclaim it.
    """
    config, compose_calls, removed = _wipe_recording_repo(tmp_path, monkeypatch)

    assert worktree_remove(config, slug="ds_180", yes=True) == 0

    assert compose_calls, "no compose call — the stack would be left running"
    assert "down" in compose_calls[0]
    assert "-v" not in compose_calls[0], "plain remove must not destroy volumes"
    assert removed == [], f"plain remove must not delete volumes, removed {removed}"
    assert not (tmp_path / ".worktrees" / "ds_180").exists()

    err = capsys.readouterr().err
    assert "--wipe" in err, "must point at the flag that would have reclaimed the volumes"
    assert "docker volume rm" in err, "must give a reclaim command for the kept volumes"


def test_worktree_remove_keep_stack_preserves_the_old_behaviour(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch for anyone who was relying on remove leaving the stack up."""
    config, compose_calls, removed = _wipe_recording_repo(tmp_path, monkeypatch)

    assert worktree_remove(config, slug="ds_180", keep_stack=True, yes=True) == 0

    assert compose_calls == [], f"--keep-stack must not touch the stack, got {compose_calls}"
    assert removed == []
    assert not (tmp_path / ".worktrees" / "ds_180").exists()


def test_worktree_remove_keeps_the_checkout_when_the_stack_will_not_stop(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed `down` must not retire the checkout — that is how orphans are born."""
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="ds_180", dry_run=False, seed=False) == 0

    monkeypatch.setattr(
        "catalpa_tooling.worktree._compose",
        lambda *a, **k: subprocess.CompletedProcess(list(a), 1),
    )

    assert worktree_remove(config, slug="ds_180", yes=True) != 0
    assert (tmp_path / ".worktrees" / "ds_180").is_dir(), "checkout must survive a failed down"


# --- issue #62: the worktree stack must run the worktree's code, whatever the cwd -------------


def _worktree_with_stack(tmp_path: Path) -> tuple[object, Path]:
    """A main checkout plus a real worktree checkout, both with a compose file."""
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="mount_probe", dry_run=False, seed=False) == 0
    return config, tmp_path / ".worktrees" / "mount_probe"


@pytest.mark.parametrize("run_from", ["main", "worktree"])
def test_worktree_up_targets_the_worktree_compose_file_from_any_cwd(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    run_from: str,
) -> None:
    """`dk worktree up` must not depend on the process cwd.

    `_compose` never chdirs, so a repo-relative `-f` resolved against the caller's cwd. Driven
    from the main checkout — the documented workflow — the worktree stack picked up the main
    checkout's compose file and, through its relative binds, the main checkout's *source code*,
    while COMPOSE_PROJECT_NAME still said "worktree". Silent, and every cheap identity signal
    looked correct.
    """
    from catalpa_tooling.worktree import worktree_up

    config, wt_root = _worktree_with_stack(tmp_path)
    monkeypatch.chdir(tmp_path if run_from == "main" else wt_root)

    compose_calls: list[str] = []

    def fake_compose(compose_file, *args, **kwargs):
        compose_calls.append(str(compose_file))
        return subprocess.CompletedProcess([str(compose_file), *args], 0)

    monkeypatch.setattr("catalpa_tooling.compose._compose", fake_compose)
    monkeypatch.setattr("catalpa_tooling.env_handlers._compose", fake_compose)
    monkeypatch.setattr("catalpa_tooling.local_proxy.ensure_proxy_running", lambda **_: 0)
    monkeypatch.setattr("catalpa_tooling.worktree.ensure_proxy_running", lambda **_: 0)
    monkeypatch.setattr(
        "catalpa_tooling.env_handlers.ensure_stack_matches_checkout", lambda *a, **k: (0, None)
    )
    monkeypatch.setattr(
        "catalpa_tooling.env_handlers.sync_local_proxy_for_compose_action", lambda *a, **k: 0
    )
    monkeypatch.setattr("catalpa_tooling.env_handlers._ensure_stack_volumes", lambda *a, **k: 0)
    monkeypatch.setattr(
        "catalpa_tooling.env_handlers._ensure_local_stack_images_built", lambda *a, **k: 0
    )
    monkeypatch.setattr("catalpa_tooling.env_handlers.materialize_configs", lambda *a, **k: 0)

    assert worktree_up(config, slug="mount_probe", dry_run=False) == 0

    assert compose_calls, "no compose invocation captured"
    target = Path(compose_calls[0])
    assert target.is_absolute(), f"relative -f resolves against cwd, not the worktree: {target}"
    assert target == (wt_root / "compose.dev.yaml").resolve(), (
        f"worktree stack pointed at {target}, not the worktree's own compose file"
    )


def test_resolve_compose_file_is_absolute_and_rooted_at_its_config(
    tmp_path: Path, isolated_tooling: None
) -> None:
    """The main checkout and a worktree must resolve to their *own* compose files."""
    from catalpa_tooling.managed_deploy_env import resolve_compose_file_from_info

    config, wt_root = _worktree_with_stack(tmp_path)
    info = yaml.safe_load(
        (tmp_path / "docker" / "envs" / "dev" / "info.yaml").read_text(encoding="utf-8")
    )
    wt_config = load_project_config(wt_root)

    main_target = resolve_compose_file_from_info(info, config)
    wt_target = resolve_compose_file_from_info(info, wt_config)

    assert Path(main_target).is_absolute() and Path(wt_target).is_absolute()
    assert Path(main_target) == (tmp_path / "compose.dev.yaml").resolve()
    assert Path(wt_target) == (wt_root / "compose.dev.yaml").resolve()
    assert main_target != wt_target, "both checkouts resolved to the same compose file"


def test_worktree_up_dry_run_reports_the_worktree_identity(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """`--dry-run` is the natural "which stack am I pointed at?" check.

    The early exit skips the context load to avoid a SOPS decrypt, and the overlay is applied in
    there — so it answered with the *main* env's origin while the real run used the worktree's.
    That actively obstructed diagnosing the mount bug above.
    """
    from catalpa_tooling.worktree import worktree_up

    config, wt_root = _worktree_with_stack(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("catalpa_tooling.worktree.ensure_proxy_running", lambda **_: 0)

    assert worktree_up(config, slug="mount_probe", dry_run=True) == 0

    err = capsys.readouterr().err
    assert "worktree overlay:" in err, "no hint that an overlay applies to this stack"
    assert "app_compose_dev_mount_probe" in err
    assert "minimal-dev-mount-probe.localdev.temp.build" in err


def test_worktree_up_dry_run_does_not_write_agents_local(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dry run must not touch the tree; the refreshed content differs when paths have moved."""
    from catalpa_tooling.worktree import worktree_up

    config, wt_root = _worktree_with_stack(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("catalpa_tooling.worktree.ensure_proxy_running", lambda **_: 0)

    agents = wt_root / AGENTS_LOCAL_NAME
    agents.write_text("sentinel\n", encoding="utf-8")
    before = agents.stat().st_mtime_ns

    assert worktree_up(config, slug="mount_probe", dry_run=True) == 0

    assert agents.read_text(encoding="utf-8") == "sentinel\n"
    assert agents.stat().st_mtime_ns == before


def _seedable_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slug: str = "feat-a"
) -> tuple[ProjectConfigT, Path]:
    """Main checkout with one worktree, and `_compose` stubbed out."""
    monkeypatch.setattr(
        "catalpa_tooling.worktree._compose",
        lambda *a, **k: subprocess.CompletedProcess(list(a), 0),
    )
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug=slug, dry_run=False, seed=False) == 0
    return config, tmp_path / ".worktrees" / sanitize_worktree_slug(slug)


def test_worktree_seed_media_copies_between_volumes(
    tmp_path: Path, isolated_tooling: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy reaches the stack's named volume, not a host directory (#88)."""
    config, wt = _seedable_worktree(tmp_path, monkeypatch)
    calls: list[dict] = []
    src_media, dst_media = _patch_media_transfer(monkeypatch, calls)

    assert worktree_seed(config, slug="feat_a", do_db=False, yes=True) == 0

    assert len(calls) == 1
    call = calls[0]
    assert call["src_media"] == src_media
    assert call["dst_media"] == dst_media
    # The destination env must name the worktree's compose project, or the copy lands
    # in the main checkout's volume.
    assert call["dst_env"]["COMPOSE_PROJECT_NAME"] == "app_compose_dev_feat_a"
    assert call["src_env"]["COMPOSE_PROJECT_NAME"] != "app_compose_dev_feat_a"
    # The staging directory lives under the session tmpdir and is cleaned up afterwards.
    assert call["media_dir"].name == "media"
    assert not call["media_dir"].exists()


def test_worktree_seed_skips_an_empty_source_without_failing(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A project with no media is legitimate: skip, say so, exit 0 (#88)."""
    config, _wt = _seedable_worktree(tmp_path, monkeypatch)
    calls: list[dict] = []
    _patch_media_transfer(monkeypatch, calls, source_has_content=False)

    capsys.readouterr()
    assert worktree_seed(config, slug="feat_a", do_db=False, yes=True) == 0

    assert calls == []
    err = capsys.readouterr().err
    assert "skipped" in err
    assert "app_compose_dev_django_media" in err
    assert "media copy done" not in err


def test_worktree_seed_summary_reports_each_step(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The final line is built from the outcomes, not asserted (#88)."""
    config, _wt = _seedable_worktree(tmp_path, monkeypatch)
    _patch_media_transfer(monkeypatch, [], source_has_content=False)

    capsys.readouterr()
    assert worktree_seed(config, slug="feat_a", do_db=False, yes=True) == 0
    err = capsys.readouterr().err
    assert "worktree seed: complete (db: not requested; media: skipped" in err

    _patch_media_transfer(monkeypatch, [], source_has_content=True)
    monkeypatch.setattr(
        "catalpa_tooling.worktree._seed_database",
        lambda *a, **k: (0, "restored"),
    )
    capsys.readouterr()
    assert worktree_seed(config, slug="feat_a", do_db=True, yes=True) == 0
    err = capsys.readouterr().err
    assert "worktree seed: complete (db: restored; media: copied volume " in err


def test_worktree_seed_dry_run_prints_both_storages(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _wt = _seedable_worktree(tmp_path, monkeypatch)
    calls: list[dict] = []
    _patch_media_transfer(monkeypatch, calls)

    capsys.readouterr()
    assert worktree_seed(config, slug="feat_a", do_db=False, dry_run=True, yes=True) == 0
    err = capsys.readouterr().err
    assert "volume app_compose_dev_django_media" in err
    assert "volume app_compose_dev_feat_a_django_media" in err
    assert calls == []


def test_worktree_seed_media_failure_is_fatal(
    tmp_path: Path, isolated_tooling: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _wt = _seedable_worktree(tmp_path, monkeypatch)
    _patch_media_transfer(monkeypatch, [], transfer_rc=3)
    assert worktree_seed(config, slug="feat_a", do_db=False, yes=True) == 3


def test_media_seeded_reads_the_stack_volume(
    tmp_path: Path, isolated_tooling: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`seeded` used to read the host tree, so a volume project always reported false."""
    from catalpa_tooling.worktree import _media_seeded

    config, wt = _seedable_worktree(tmp_path, monkeypatch)
    del config
    wt_config = load_project_config(wt)
    overlay = load_worktree_overlay(wt)
    assert overlay is not None

    storage = MediaStorage(MediaStorageKind.VOLUME, "app_compose_dev_feat_a_django_media")
    monkeypatch.setattr(
        "catalpa_tooling.worktree._worktree_media_storage",
        lambda *a, **k: (storage, {"COMPOSE_PROJECT_NAME": "app_compose_dev_feat_a"}),
    )

    # The host tree is empty either way; only the volume decides.
    for has_content, expected in ((True, True), (False, False), (None, False)):
        monkeypatch.setattr(
            "catalpa_tooling.worktree.media_storage_has_content",
            lambda s, e, **k: has_content,
        )
        assert _media_seeded(wt_config, overlay) is expected


def _rev_parse(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "rev-parse", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_worktree_create_records_base_ref_and_commit(
    tmp_path: Path, isolated_tooling: None, capsys: pytest.CaptureFixture[str]
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    main_branch = _rev_parse(tmp_path, "--abbrev-ref", "HEAD")
    subprocess.run(["git", "branch", "release-x"], cwd=tmp_path, check=True)
    config = load_project_config(tmp_path)

    assert worktree_create(config, slug="from-head", dry_run=False, seed=False) == 0
    assert worktree_create(
        config, slug="from-rel", base_branch="release-x", dry_run=False, seed=False
    ) == 0

    head = load_worktree_overlay(tmp_path / ".worktrees" / "from_head")
    assert head is not None
    assert head.base_ref == main_branch  # HEAD resolves to the checked-out branch
    assert head.base_commit == _rev_parse(tmp_path, "HEAD")
    rel = load_worktree_overlay(tmp_path / ".worktrees" / "from_rel")
    assert rel is not None
    assert rel.base_ref == "release-x"
    assert rel.base_commit == _rev_parse(tmp_path, "release-x")


def test_worktree_create_detached_base_records_commit_only(
    tmp_path: Path, isolated_tooling: None
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    sha = _rev_parse(tmp_path, "HEAD")
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=tmp_path, check=True)

    assert worktree_create(
        load_project_config(tmp_path), slug="det", dry_run=False, seed=False
    ) == 0
    overlay = load_worktree_overlay(tmp_path / ".worktrees" / "det")
    assert overlay is not None
    assert overlay.base_ref is None
    assert overlay.base_commit == sha


def test_worktree_create_existing_branch_records_no_base(
    tmp_path: Path, isolated_tooling: None
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    subprocess.run(["git", "branch", "worktree/old"], cwd=tmp_path, check=True)

    assert worktree_create(
        load_project_config(tmp_path), slug="old", dry_run=False, seed=False
    ) == 0
    raw = yaml.safe_load((tmp_path / ".worktrees" / "old" / ".catalpa-worktree.yaml").read_text())
    assert "base_ref" not in raw and "base_commit" not in raw


def test_marker_without_base_fields_still_loads(tmp_path: Path) -> None:
    (tmp_path / ".catalpa-worktree.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "slug": "legacy",
                "base_env": "dev",
                "compose_project_name": "app_compose_dev_legacy",
                "site_origin": "https://minimal-dev-legacy.localdev.temp.build",
            }
        ),
        encoding="utf-8",
    )
    overlay = load_worktree_overlay(tmp_path)
    assert overlay is not None
    assert overlay.base_ref is None and overlay.base_commit is None


def test_worktree_context_json_stdout_is_only_json(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_minimal_tooling_tree(tmp_path)
    write_dev_env(tmp_path)
    git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="ctx", dry_run=False, seed=False) == 0
    monkeypatch.setattr("catalpa_tooling.worktree.worktree_stack_status", lambda *a, **k: "stopped")
    monkeypatch.setattr("catalpa_tooling.worktree._media_seeded", lambda *a, **k: False)
    capsys.readouterr()

    # real _git_head_info: its ``$ git rev-parse …`` echoes must not reach stdout
    assert worktree_context(config, slug="ctx", as_json=True) == 0
    out = capsys.readouterr()
    import json

    assert json.loads(out.out)["branch"] == "worktree/ctx"
    assert "$ git rev-parse" in out.err
