"""Tests for worktree overlays and ``dk worktree`` helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from catalpa_tooling.cli.dk_argv import peel_worktree_flag
from catalpa_tooling.config import load_project_config
from catalpa_tooling.dk_cli import _load_config_for_argv
from catalpa_tooling.managed_deploy_env import load_managed_deploy_context
from catalpa_tooling.worktree import (
    AGENTS_LOCAL_NAME,
    ensure_worktree_gitignore,
    media_dir_for_config,
    resolve_worktree_root,
    worktree_context,
    worktree_create,
    worktree_list,
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
from tests.helpers import write_minimal_tooling_tree


def _write_dev_env(repo: Path) -> None:
    env_dir = repo / "docker" / "envs" / "dev"
    env_dir.mkdir(parents=True, exist_ok=True)
    (repo / "compose.dev.yaml").write_text("services: {}\n", encoding="utf-8")
    info = {
        "compose_file": "compose.dev.yaml",
        "credentials_decrypt_optional": True,
        "site_origin": "https://minimal-dev.localdev.temp.build",
        "local_proxy": {"roles": ["admin", "stats"]},
        "env": {
            "compose_project_name": "app_compose_dev",
            "bero_origin": "https://minimal-dev.localdev.temp.build",
            "django_origin": "https://admin.minimal-dev.localdev.temp.build",
            "metabase_origin": "https://stats.minimal-dev.localdev.temp.build",
            "caddy_site_address": "http://minimal-dev.localdev.temp.build",
            "postgres_password": "dev",
        },
    }
    (env_dir / "info.yaml").write_text(yaml.safe_dump(info), encoding="utf-8")
    tooling = (repo / "tooling.yaml").read_text(encoding="utf-8")
    if "credentials_optional_envs:" in tooling and "- dev" not in tooling:
        tooling = tooling.replace(
            "credentials_optional_envs:\n      - local\n",
            "credentials_optional_envs:\n      - local\n      - dev\n",
        )
        (repo / "tooling.yaml").write_text(tooling, encoding="utf-8")


def _git_init_commit(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )


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
    _write_dev_env(tmp_path)
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
    _write_dev_env(tmp_path)
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
    tmp_path: Path, isolated_tooling: None
) -> None:
    write_minimal_tooling_tree(tmp_path)
    _write_dev_env(tmp_path)
    _git_init_commit(tmp_path)

    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="feat-a", dry_run=False, seed=False) == 0
    wt = tmp_path / ".worktrees" / "feat_a"
    assert wt.is_dir()
    assert load_worktree_overlay(wt) is not None
    assert ".worktrees/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")

    assert worktree_list(config) == 0

    wt_config = load_project_config(wt)
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "x.txt").write_text("hi", encoding="utf-8")
    assert worktree_seed(
        wt_config, do_db=False, do_media=True, dry_run=True, yes=True
    ) == 0
    assert worktree_seed(
        wt_config, do_db=False, do_media=True, dry_run=False, yes=True
    ) == 0
    assert (wt / "media" / "x.txt").read_text(encoding="utf-8") == "hi"
    assert media_dir_for_config(wt_config) == wt / "media"

    (wt / "media" / "x.txt").unlink()
    assert worktree_seed(
        config, slug="feat_a", do_db=False, do_media=True, dry_run=False, yes=True
    ) == 0
    assert (wt / "media" / "x.txt").read_text(encoding="utf-8") == "hi"

    from catalpa_tooling.worktree import worktree_remove

    assert worktree_remove(config, slug="feat-a", yes=True) == 0
    assert not wt.exists()


def test_worktree_create_inits_submodules(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_tooling_tree(tmp_path)
    _write_dev_env(tmp_path)
    (tmp_path / ".gitmodules").write_text(
        '[submodule "bero"]\n\tpath = bero\n\turl = https://example.com/bero.git\n',
        encoding="utf-8",
    )
    _git_init_commit(tmp_path)
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
    _write_dev_env(tmp_path)
    (tmp_path / ".gitmodules").write_text('[submodule "bero"]\npath = bero\n', encoding="utf-8")
    bero = tmp_path / "bero"
    bero.mkdir()
    (bero / ".git").mkdir()
    _git_init_commit(tmp_path)
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
    _write_dev_env(tmp_path)
    _git_init_commit(tmp_path)
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
    _write_dev_env(tmp_path)
    _git_init_commit(tmp_path)
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
    _write_dev_env(tmp_path)
    _git_init_commit(tmp_path)
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
    _write_dev_env(tmp_path)
    _git_init_commit(tmp_path)
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
    _write_dev_env(tmp_path)
    _git_init_commit(tmp_path)
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
    _write_dev_env(tmp_path)
    _git_init_commit(tmp_path)
    config = load_project_config(tmp_path)
    assert worktree_create(config, slug="ctx", dry_run=False, seed=False) == 0

    monkeypatch.setattr(
        "catalpa_tooling.worktree.worktree_stack_status",
        lambda *a, **k: "running",
    )
    monkeypatch.setattr(
        "catalpa_tooling.worktree._media_dir_seeded",
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
    assert "head_commit" in payload
    assert "worktree" in payload


def test_list_and_status_tolerate_docker_failure(
    tmp_path: Path,
    isolated_tooling: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    write_minimal_tooling_tree(tmp_path)
    _write_dev_env(tmp_path)
    _git_init_commit(tmp_path)
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
    _write_dev_env(tmp_path)
    config = load_project_config(tmp_path)

    called: list[str] = []

    def fake_up(config, *, slug=None, dry_run=False):
        called.append(slug or "")
        return 0

    monkeypatch.setattr("catalpa_tooling.worktree_cli.worktree_up", fake_up)
    ns = argparse.Namespace(worktree_command="up", slug="feat", dry_run=False)
    assert cmd_worktree(ns, config) == 0
    assert called == ["feat"]
