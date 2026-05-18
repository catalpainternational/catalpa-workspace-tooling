"""Image registry config and tagging for dk push."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from catalpa_tooling.run_cmd import run as run_cmd

if TYPE_CHECKING:
    from catalpa_tooling.config import ProjectConfig


def _git_tag_at_head(cwd: Path) -> str | None:
    """If any tag points at HEAD, return the preferred one (highest version:refname sort), else None."""
    r = run_cmd(
        ["git", "tag", "--points-at", "HEAD", "--sort=-version:refname"],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        print_cmd=False,
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    line = (r.stdout or "").strip().splitlines()[0].strip()
    return line or None


def _git_worktree_dirty(cwd: Path) -> bool:
    r = run_cmd(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        print_cmd=False,
    )
    return r.returncode == 0 and bool((r.stdout or "").strip())


def _default_image_tag(repo_root: Path | None = None) -> str:
    """Return a tag for images: git tag at HEAD if any, else branch name, else git describe, else 'latest'."""
    cwd = repo_root if repo_root is not None else Path.cwd()
    tag_at_head = _git_tag_at_head(cwd)
    if tag_at_head:
        base = tag_at_head.replace("/", "-")
        if _git_worktree_dirty(cwd):
            return f"{base}-dirty"
        return base
    branch_r = run_cmd(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        print_cmd=False,
    )
    if branch_r.returncode == 0 and branch_r.stdout:
        branch = branch_r.stdout.strip()
        if branch and branch != "HEAD":
            return branch.replace("/", "-")
    describe_r = run_cmd(
        ["git", "describe", "--tags", "--always", "--dirty"],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        print_cmd=False,
    )
    if describe_r.returncode == 0 and describe_r.stdout:
        return describe_r.stdout.strip().replace("/", "-")
    return "latest"


def _load_images_config(config: ProjectConfig) -> dict:
    """Load project-level image config from ``paths.deploy.images_config``."""
    path = config.images_config_path
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _image_registry_from_config(images_config: dict, config: ProjectConfig) -> str:
    key = config.stack.images.registry_key
    return (images_config.get(key) or images_config.get("image_registry") or "").rstrip("/")


def _github_repository(repo_root: Path) -> str | None:
    """Return 'owner/repo' from git remote origin URL, or None if not a GitHub repo."""
    r = run_cmd(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
        print_cmd=False,
    )
    if r.returncode != 0 or not r.stdout:
        return None
    url = r.stdout.strip().rstrip("/")
    if url.startswith("https://github.com/"):
        path = url.removeprefix("https://github.com/").removesuffix(".git")
    elif url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:").removesuffix(".git")
    else:
        return None
    if "/" in path and path.count("/") >= 1:
        return path
    return None
