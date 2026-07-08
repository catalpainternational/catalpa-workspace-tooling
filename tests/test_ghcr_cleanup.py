"""Tests for GHCR cleanup planning and API helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from catalpa_tooling.clean_images import clean_images
from catalpa_tooling.config import load_project_config
from catalpa_tooling.ghcr_cleanup import (
    PackageVersion,
    _parse_older_than,
    _parse_registry_owner,
    collect_deploy_tags,
    plan_deletions,
    resolve_ghcr_cleanup_plan,
    run_cleanup,
    tag_matches_any,
    version_is_excluded,
)
from tests.helpers import write_minimal_tooling_tree


def _write_images_yaml(root: Path, content: str) -> None:
    (root / "docker" / "images.yaml").write_text(content, encoding="utf-8")


def _write_env_info(root: Path, env_name: str, content: str) -> None:
    env_dir = root / "docker" / "envs" / env_name
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "info.yaml").write_text(content, encoding="utf-8")


def test_parse_registry_owner() -> None:
    assert _parse_registry_owner("ghcr.io/catalpainternational") == "catalpainternational"
    assert _parse_registry_owner("ghcr.io/example/app") == "example"


def test_parse_older_than() -> None:
    assert _parse_older_than("180 days") == timedelta(days=180)
    assert _parse_older_than("2 weeks") == timedelta(weeks=2)


def test_resolve_plan_from_fixture(minimal_project) -> None:
    _write_images_yaml(
        minimal_project.repo_root,
        """
image_registry: ghcr.io/example/app
ghcr_cleanup:
  keep_n_tagged: 5
  older_than: 30 days
  extra_exclude_tags: [hotfix-*]
""",
    )
    _write_env_info(minimal_project.repo_root, "staging", "image_tag: v1.0.0\n")
    plan = resolve_ghcr_cleanup_plan(minimal_project)
    assert plan.owner == "example"
    assert plan.packages == ("app-web", "app-proxy", "app-db")
    assert plan.keep_n_tagged == 5
    assert plan.older_than == timedelta(days=30)
    assert "v1.0.0" in plan.exclude_tags
    assert "hotfix-*" in plan.exclude_tags


def test_collect_deploy_tags_from_credentials(monkeypatch, minimal_project) -> None:
    _write_env_info(minimal_project.repo_root, "prod", "description: prod\n")
    creds_path = minimal_project.repo_root / "docker" / "envs" / "prod" / "credentials.yaml"
    creds_path.write_text("encrypted\n", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        from types import SimpleNamespace

        if cmd[:2] == ["sops", "-d"]:
            return SimpleNamespace(returncode=0, stdout="tag: v3.0.0\n", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("catalpa_tooling.ghcr_cleanup.run_cmd", fake_run)
    tags = collect_deploy_tags(minimal_project)
    assert tags == {"v3.0.0"}


def test_collect_deploy_tags(minimal_project) -> None:
    _write_env_info(minimal_project.repo_root, "staging", "image_tag: v2.1.0\n")
    _write_env_info(minimal_project.repo_root, "prod", "image_tag: v2.0.0\n")
    tags = collect_deploy_tags(minimal_project)
    assert tags == {"v2.1.0", "v2.0.0"}


def test_tag_wildcards() -> None:
    assert tag_matches_any("v1.2.3", ("v*",))
    assert tag_matches_any("hotfix-1", ("hotfix-*",))
    assert not tag_matches_any("main", ("v*",))


def test_version_is_excluded() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    version = PackageVersion("app-web", 1, ("v1.0.0",), now)
    assert version_is_excluded(version, ("v*",))
    assert not version_is_excluded(version, ("hotfix-*",))


def test_plan_deletions_retention() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    plan = resolve_ghcr_cleanup_plan(
        load_project_config(_make_project_root()),
        keep_n_tagged=2,
        older_than="90 days",
        delete_untagged=True,
        packages=("app-web",),
        extra_exclude_tags=("v9.9.9",),
    )
    versions = [
        PackageVersion("app-web", 1, ("v9.9.9",), now - timedelta(days=400)),
        PackageVersion("app-web", 2, ("v1.0.0",), now - timedelta(days=200)),
        PackageVersion("app-web", 3, ("v1.1.0",), now - timedelta(days=150)),
        PackageVersion("app-web", 4, ("v1.2.0",), now - timedelta(days=120)),
        PackageVersion("app-web", 5, ("v2.0.0",), now - timedelta(days=10)),
        PackageVersion("app-web", 6, tuple(), now - timedelta(days=5)),
    ]
    staged = plan_deletions(plan, {"app-web": versions}, now=now)
    ids = {item.version_id for item in staged}
    assert 1 not in ids  # excluded deploy tag
    assert 5 not in ids  # within older_than window
    assert 6 in ids  # untagged
    assert 2 in ids  # old, outside keep-n=2
    assert 3 not in ids  # kept as 2nd newest old tagged
    assert 4 not in ids  # kept as newest old tagged


def test_run_cleanup_dry_run_does_not_delete(minimal_project) -> None:
    _write_images_yaml(minimal_project.repo_root, "image_registry: ghcr.io/example/app\n")
    deleted: list[int] = []

    def fake_list(owner: str, package: str, token: str) -> list[PackageVersion]:
        return [
            PackageVersion(package, 99, tuple(), datetime(2024, 1, 1, tzinfo=UTC)),
        ]

    def fake_delete(owner: str, package: str, version_id: int, token: str) -> None:
        deleted.append(version_id)

    plan = resolve_ghcr_cleanup_plan(minimal_project)
    rc = run_cleanup(
        plan,
        dry_run=True,
        token="test-token",
        list_versions=fake_list,
        delete_version=fake_delete,
    )
    assert rc == 0
    assert deleted == []


def test_run_cleanup_apply_deletes_staged(minimal_project) -> None:
    _write_images_yaml(minimal_project.repo_root, "image_registry: ghcr.io/example/app\n")
    deleted: list[int] = []

    def fake_list(owner: str, package: str, token: str) -> list[PackageVersion]:
        return [
            PackageVersion(package, 42, tuple(), datetime(2024, 1, 1, tzinfo=UTC)),
        ]

    def fake_delete(owner: str, package: str, version_id: int, token: str) -> None:
        deleted.append(version_id)

    plan = resolve_ghcr_cleanup_plan(minimal_project)
    rc = run_cleanup(
        plan,
        dry_run=False,
        token="test-token",
        list_versions=fake_list,
        delete_version=fake_delete,
    )
    assert rc == 0
    assert deleted == [42] * len(plan.packages)


def test_clean_images_cli_dry_run(minimal_project, capsys) -> None:
    _write_images_yaml(minimal_project.repo_root, "image_registry: ghcr.io/example/app\n")

    def fake_list(owner: str, package: str, token: str) -> list[PackageVersion]:
        return []

    plan = resolve_ghcr_cleanup_plan(minimal_project)

    def fake_run_cleanup(*args, **kwargs):
        kwargs["list_versions"] = fake_list
        return run_cleanup(*args, **kwargs)

    import catalpa_tooling.clean_images as clean_images_mod

    original = clean_images_mod.run_cleanup
    clean_images_mod.run_cleanup = fake_run_cleanup
    try:
        rc = clean_images(config=minimal_project, token="test-token")
    finally:
        clean_images_mod.run_cleanup = original

    assert rc == 0
    err = capsys.readouterr().err
    assert "dry-run" in err
    assert plan.owner in err


def _make_project_root() -> Path:
    root = Path("/tmp/ghcr-cleanup-test")  # noqa: S108
    write_minimal_tooling_tree(root)
    _write_images_yaml(root, "image_registry: ghcr.io/example/app\n")
    return root
