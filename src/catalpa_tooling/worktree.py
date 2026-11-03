"""``dk worktree`` — create/list/info/remove/seed isolated git worktrees."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from catalpa_tooling.compose import _compose
from catalpa_tooling.config import ProjectConfig, load_project_config
from catalpa_tooling.local_proxy import ensure_proxy_running
from catalpa_tooling.managed_deploy_env import load_managed_deploy_context
from catalpa_tooling.media_storage import (
    MediaStorage,
    MediaStorageError,
    MediaStorageKind,
    StackVolumes,
    resolve_media_storage,
    resolve_stack_volumes,
)
from catalpa_tooling.media_transfer import run_transfer_media
from catalpa_tooling.pgbackrest_db import (
    compose_pg_restore_extras_for_config,
    ensure_db_service_running,
    run_drop_create_app_database,
    run_pg_dump_to_file,
    run_pg_restore,
)
from catalpa_tooling.pgbackrest_volume_config import remove_all_external_stack_volumes
from catalpa_tooling.post_db_restore import (
    post_db_restore_applies,
    run_post_db_restore_manage_commands,
)
from catalpa_tooling.restic_files import resolve_env_with_compose_project
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.worktree_overlay import (
    DEFAULT_BASE_ENV,
    WORKTREE_MARKER_NAME,
    WORKTREES_DIRNAME,
    WorktreeOverlay,
    WorktreeOverlayError,
    build_worktree_overlay,
    load_worktree_overlay,
    sanitize_worktree_slug,
    worktree_marker_path,
    worktrees_dir,
    write_worktree_overlay,
)

AGENTS_LOCAL_NAME = "AGENTS.local.md"
_GITIGNORE_ENTRIES = (f"{WORKTREES_DIRNAME}/", WORKTREE_MARKER_NAME, AGENTS_LOCAL_NAME)


def _git(
    *args: str,
    cwd: Path,
    check: bool = True,
    capture: bool = False,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *args]
    if dry_run:
        print(f"dry-run: {' '.join(cmd)} (cwd={cwd})", file=sys.stderr)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return run_cmd(
        cmd,
        cwd=str(cwd),
        check=check,
        capture_output=capture,
        text=True,
        print_cmd=True,
    )


def _gitignore_has_entry(text: str, entry: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == entry or stripped.rstrip("/") == entry.rstrip("/"):
            return True
    return False


def ensure_worktree_gitignore(repo_root: Path, *, dry_run: bool = False) -> list[str]:
    """Append ``.worktrees/`` and marker patterns to ``.gitignore`` if missing."""
    path = repo_root / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    missing = [e for e in _GITIGNORE_ENTRIES if not _gitignore_has_entry(existing, e)]
    if not missing:
        return []
    if dry_run:
        print(f"dry-run: append to {path}: {missing}", file=sys.stderr)
        return missing
    block = "\n".join(
        [
            "",
            "# catalpa-workspace-tooling isolated worktrees",
            *missing,
            "",
        ]
    )
    if existing and not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + block, encoding="utf-8")
    return missing


def _branch_exists(repo_root: Path, name: str) -> bool:
    result = _git(
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/heads/{name}",
        cwd=repo_root,
        check=False,
        capture=True,
    )
    return result.returncode == 0


def _default_branch_name(slug: str) -> str:
    return f"worktree/{sanitize_worktree_slug(slug)}"


def _has_gitmodules(repo_root: Path) -> bool:
    return (repo_root / ".gitmodules").is_file()


def _gitmodules_paths(repo_root: Path) -> list[str]:
    """Return submodule paths listed in ``.gitmodules`` (order preserved)."""
    gm = repo_root / ".gitmodules"
    if not gm.is_file():
        return []
    paths: list[str] = []
    for match in re.finditer(r"(?m)^\s*path\s*=\s*(.+?)\s*$", gm.read_text(encoding="utf-8")):
        path = match.group(1).strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _is_usable_submodule_reference(path: Path) -> bool:
    """True when ``path`` looks like an initialized submodule checkout we can ``--reference``."""
    if not path.is_dir():
        return False
    git_meta = path / ".git"
    return git_meta.is_dir() or git_meta.is_file()


def _submodule_update_cmd(
    *,
    shallow: bool,
    reference: Path | None = None,
    path: str | None = None,
) -> list[str]:
    cmd = ["submodule", "update", "--init", "--recursive"]
    if shallow:
        cmd.extend(["--depth", "1"])
    if reference is not None:
        cmd.extend(["--reference", str(reference.resolve())])
    if path is not None:
        cmd.extend(["--", path])
    return cmd


def _init_one_submodule(
    worktree_root: Path,
    *,
    path: str,
    reference: Path | None,
    shallow: bool,
    dry_run: bool = False,
) -> int:
    """Init one submodule; prefer ``--reference`` from the main checkout when available."""
    if reference is not None:
        cmd = _submodule_update_cmd(shallow=shallow, reference=reference, path=path)
        label = " ".join(cmd)
        print(
            f"worktree create: initializing submodule {path!r} "
            f"from local {reference} (git {label}) …",
            file=sys.stderr,
        )
        result = _git(*cmd, cwd=worktree_root, check=False, dry_run=dry_run)
        if result.returncode == 0:
            return 0
        print(
            f"worktree create: local reference for {path!r} failed; "
            "retrying without --reference …",
            file=sys.stderr,
        )

    cmd = _submodule_update_cmd(shallow=shallow, path=path)
    label = " ".join(cmd)
    print(f"worktree create: initializing submodule {path!r} (git {label}) …", file=sys.stderr)
    result = _git(*cmd, cwd=worktree_root, check=False, dry_run=dry_run)
    if result.returncode != 0:
        print(f"dk worktree create: git {label} failed.", file=sys.stderr)
        return result.returncode or 1
    return 0


def _init_worktree_submodules(
    worktree_root: Path,
    *,
    main_root: Path,
    shallow: bool = True,
    dry_run: bool = False,
) -> int:
    """Init submodules in the new worktree, preferring local checkouts from ``main_root``.

    For each path in ``.gitmodules``, if ``main_root/<path>`` is an initialized
    submodule, clone with ``git submodule update --reference`` (objects from the
    main checkout — fast / offline-friendly). Otherwise fetch from the remote.
    Default is shallow (``--depth 1``); pass ``shallow=False`` for full history.
    """
    if not _has_gitmodules(worktree_root):
        return 0

    paths = _gitmodules_paths(worktree_root)
    if not paths:
        # Unusual .gitmodules with no path= lines — fall back to bulk update.
        cmd = _submodule_update_cmd(shallow=shallow)
        label = " ".join(cmd)
        print(f"worktree create: initializing submodules (git {label}) …", file=sys.stderr)
        result = _git(*cmd, cwd=worktree_root, check=False, dry_run=dry_run)
        if result.returncode != 0:
            print(f"dk worktree create: git {label} failed.", file=sys.stderr)
            return result.returncode or 1
        return 0

    for path in paths:
        ref_candidate = (main_root / path).resolve()
        reference = ref_candidate if _is_usable_submodule_reference(ref_candidate) else None
        if reference is None:
            print(
                f"worktree create: no local submodule at {main_root / path}; "
                "will fetch remote",
                file=sys.stderr,
            )
        rc = _init_one_submodule(
            worktree_root,
            path=path,
            reference=reference,
            shallow=shallow,
            dry_run=dry_run,
        )
        if rc != 0:
            return rc
    return 0


def resolve_parent_repo_root(config: ProjectConfig) -> Path:
    """Main checkout for a worktree (marker parent, or repo root if not a worktree)."""
    overlay = load_worktree_overlay(config.repo_root)
    if overlay and overlay.parent_repo_root:
        parent = Path(overlay.parent_repo_root)
        if parent.is_dir():
            return parent.resolve()
    root = config.repo_root.resolve()
    if root.parent.name == WORKTREES_DIRNAME:
        return root.parent.parent
    return root


def resolve_worktree_root(main_or_cwd_root: Path, slug: str) -> Path:
    """Return ``.worktrees/<slug>`` under the main checkout; require marker file.

    ``main_or_cwd_root`` may be the main repo or an existing worktree (parent is used).
    """
    try:
        clean = sanitize_worktree_slug(slug)
    except WorktreeOverlayError as exc:
        raise WorktreeOverlayError(str(exc)) from exc

    root = Path(main_or_cwd_root).resolve()
    # If cwd is already a worktree, resolve against its parent main.
    if load_worktree_overlay(root) is not None:
        overlay = load_worktree_overlay(root)
        assert overlay is not None
        if overlay.parent_repo_root:
            parent = Path(overlay.parent_repo_root)
            if parent.is_dir():
                root = parent.resolve()
        elif root.parent.name == WORKTREES_DIRNAME:
            root = root.parent.parent

    path = worktrees_dir(root) / clean
    if not path.is_dir():
        raise WorktreeOverlayError(
            f"worktree {clean!r} not found at {path} "
            f"(create with: dk worktree create {clean})"
        )
    marker = worktree_marker_path(path)
    if not marker.is_file():
        raise WorktreeOverlayError(
            f"worktree {clean!r} is missing {WORKTREE_MARKER_NAME} at {marker}"
        )
    return path


def media_dir_for_config(config: ProjectConfig) -> Path:
    """Host media tree: ``paths.media_dir`` or ``<repo>/media``."""
    if config.media_dir is not None:
        return config.media_dir
    return config.repo_root / "media"


def _agents_local_template_path() -> Path:
    """Bundled template (packaged under ``share/worktree/``; scripts/ mirror for editing)."""
    from importlib.resources import files

    packaged = Path(
        files("catalpa_tooling").joinpath("share/worktree/AGENTS.local.md.template")
    )
    if packaged.is_file():
        return packaged
    return (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "worktree"
        / "AGENTS.local.md.template"
    )


def write_agents_local_md(
    worktree_root: Path,
    overlay: WorktreeOverlay,
    *,
    parent_repo: Path,
    media_path: Path,
) -> None:
    """Write ``AGENTS.local.md`` at the worktree root from the bundled template."""
    template_path = _agents_local_template_path()
    if not template_path.is_file():
        raise FileNotFoundError(f"Missing worktree agent template: {template_path}")
    text = template_path.read_text(encoding="utf-8")
    replacements = {
        "${SLUG}": overlay.slug,
        "${BRANCH}": overlay.branch or "-",
        "${COMPOSE_PROJECT}": overlay.compose_project_name,
        "${SITE_ORIGIN}": overlay.site_origin,
        "${BASE_ENV}": overlay.base_env,
        "${WORKTREE_PATH}": str(worktree_root.resolve()),
        "${PARENT_REPO}": str(parent_repo.resolve()),
        "${MEDIA_PATH}": str(media_path.resolve()),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    (worktree_root / AGENTS_LOCAL_NAME).write_text(text, encoding="utf-8")


SEED_ALPINE_IMAGE = "alpine:3.21"


def _merged_docker_env(env: dict[str, str]) -> dict[str, str]:
    out = os.environ.copy()
    for key, value in env.items():
        if value is not None:
            out[key] = str(value)
    return out


def _bind_dir_has_content(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def media_storage_has_content(
    storage: MediaStorage,
    env: dict[str, str],
    *,
    alpine_image: str = SEED_ALPINE_IMAGE,
) -> bool | None:
    """True / False, or ``None`` when Docker could not answer.

    A bind mount is read directly. A named volume needs a container to look inside, because
    its mountpoint is inside the Docker VM on macOS and Windows.
    """
    if storage.kind is MediaStorageKind.BIND:
        return _bind_dir_has_content(Path(storage.location))

    from catalpa_tooling.dk_transfer import _docker_volume_exists

    if not _docker_volume_exists(storage.location, env):
        return False
    result = run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{storage.location}:/data:ro",
            alpine_image,
            "sh",
            "-c",
            "ls -A /data | head -n 1",
        ],
        env=_merged_docker_env(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if result.returncode != 0:
        return None
    return bool((result.stdout or "").strip())


def _worktree_media_storage(
    wt_config: ProjectConfig,
    overlay: WorktreeOverlay,
) -> tuple[MediaStorage, dict[str, str]] | None:
    """Resolve the worktree stack's media storage and the env that addresses it."""
    resolved = _worktree_compose_env(wt_config, overlay)
    if resolved is None:
        return None
    compose_abs, env_r = resolved
    try:
        storage = resolve_media_storage(
            compose_file=compose_abs,
            env=env_r,
            config=wt_config,
        )
    except MediaStorageError:
        return None
    return storage, env_r


def _media_seeded(wt_config: ProjectConfig, overlay: WorktreeOverlay) -> bool:
    """True when the media the worktree's stack actually serves has content.

    For a Docker project that is a named volume, not the host tree under the checkout —
    reading the host tree reported ``false`` for every volume project. See issue #88.
    """
    try:
        resolved = _worktree_media_storage(wt_config, overlay)
    except Exception:
        return False
    if resolved is None:
        return False
    storage, env_r = resolved
    try:
        return media_storage_has_content(storage, env_r) is True
    except Exception:
        return False


def _git_head_info(worktree_root: Path) -> tuple[str | None, str | None]:
    """Return ``(branch, head_commit)`` from the worktree checkout."""
    branch: str | None = None
    head: str | None = None
    br = _git(
        "rev-parse",
        "--abbrev-ref",
        "HEAD",
        cwd=worktree_root,
        check=False,
        capture=True,
    )
    if br.returncode == 0:
        branch = br.stdout.strip() or None
    rev = _git(
        "rev-parse",
        "HEAD",
        cwd=worktree_root,
        check=False,
        capture=True,
    )
    if rev.returncode == 0:
        head = rev.stdout.strip() or None
    return branch, head


def _resolve_start_ref(main_root: Path, start_ref: str) -> tuple[str | None, str | None]:
    """Return ``(base_ref, base_commit)`` for ``start_ref`` in the main checkout.

    ``base_ref`` is the symbolic name (``HEAD`` resolves to the checked-out branch);
    ``None`` when detached or unresolvable.
    """
    base_ref: str | None = None
    base_commit: str | None = None
    name = _git(
        "rev-parse",
        "--abbrev-ref",
        start_ref,
        cwd=main_root,
        check=False,
        capture=True,
    )
    if name.returncode == 0:
        resolved = name.stdout.strip()
        base_ref = resolved if resolved and resolved != "HEAD" else None
    rev = _git(
        "rev-parse",
        "--verify",
        "--quiet",
        f"{start_ref}^{{commit}}",
        cwd=main_root,
        check=False,
        capture=True,
    )
    if rev.returncode == 0:
        base_commit = rev.stdout.strip() or None
    return base_ref, base_commit


def _resolve_worktree(
    config: ProjectConfig,
    slug: str | None = None,
) -> tuple[Path, WorktreeOverlay, ProjectConfig]:
    """Return ``(worktree_root, overlay, wt_config)`` for a slug or current checkout."""
    if slug:
        wt_root = resolve_worktree_root(config.repo_root, slug)
    else:
        wt_root = config.repo_root.resolve()
        if load_worktree_overlay(wt_root) is None:
            raise WorktreeOverlayError(
                f"not inside a worktree (no {WORKTREE_MARKER_NAME}). "
                "Pass a slug or use dk --worktree <slug>."
            )

    try:
        overlay = load_worktree_overlay(wt_root)
    except WorktreeOverlayError as exc:
        raise WorktreeOverlayError(str(exc)) from exc
    if overlay is None:
        raise WorktreeOverlayError(
            f"worktree {wt_root} is missing {WORKTREE_MARKER_NAME}"
        )

    try:
        wt_config = load_project_config(wt_root)
    except Exception as exc:
        raise WorktreeOverlayError(
            f"cannot load tooling at {wt_root}: {exc}"
        ) from exc
    return wt_root, overlay, wt_config


def _worktree_compose_env(
    wt_config: ProjectConfig,
    overlay: WorktreeOverlay,
) -> tuple[str, dict[str, str]] | None:
    """Resolve compose file path and env for the remapped worktree stack."""
    ctx = load_managed_deploy_context(
        wt_config, overlay.base_env, apply_worktree=True
    )
    if ctx is None:
        return None
    env_r = resolve_env_with_compose_project(
        ctx.compose_file,
        ctx.env_add,
        config=wt_config,
        dk_env_name=overlay.base_env,
    )
    env_r["COMPOSE_PROJECT_NAME"] = overlay.compose_project_name
    compose_abs = str((wt_config.repo_root / ctx.compose_file).resolve())
    return compose_abs, env_r


def worktree_stack_status(
    wt_config: ProjectConfig,
    overlay: WorktreeOverlay,
) -> str:
    """Return ``running``, ``stopped``, or ``unknown`` for the worktree compose project."""
    try:
        resolved = _worktree_compose_env(wt_config, overlay)
        if resolved is None:
            return "unknown"
        compose_abs, env_r = resolved
        running = _compose(
            compose_abs,
            "ps",
            "--status",
            "running",
            "-q",
            check=False,
            env_add=env_r,
            capture_output=True,
        )
        if running.returncode != 0:
            return "unknown"
        if running.stdout.strip():
            return "running"
        any_ps = _compose(
            compose_abs,
            "ps",
            "-a",
            "-q",
            check=False,
            env_add=env_r,
            capture_output=True,
        )
        if any_ps.returncode != 0:
            return "unknown"
        return "stopped"
    except Exception:
        return "unknown"


def _run_worktree_env_compose(
    main_or_any_config: ProjectConfig,
    *,
    slug: str | None,
    compose_args: list[str],
    dry_run: bool,
) -> int:
    """Resolve worktree config and run ``handle_env_command`` with remapped compose."""
    from catalpa_tooling.env_handlers import handle_env_command

    try:
        wt_root, overlay, wt_config = _resolve_worktree(main_or_any_config, slug)
    except WorktreeOverlayError as exc:
        print(f"dk worktree: {exc}", file=sys.stderr)
        return 1

    ns = argparse.Namespace(
        env_name=overlay.base_env,
        env_command=None,
        implicit_compose_argv=list(compose_args),
        dry_run=dry_run,
        yes=True,
        tag=None,
    )
    return handle_env_command(ns, wt_config)


def worktree_up(
    config: ProjectConfig,
    *,
    slug: str | None = None,
    dry_run: bool = False,
) -> int:
    """Ensure local proxy and bring up the remapped worktree stack."""
    try:
        wt_root, overlay, wt_config = _resolve_worktree(config, slug)
    except WorktreeOverlayError as exc:
        print(f"dk worktree up: {exc}", file=sys.stderr)
        return 1

    parent = (
        Path(overlay.parent_repo_root).resolve()
        if overlay.parent_repo_root
        else resolve_parent_repo_root(config)
    )
    if not dry_run:
        # `--dry-run` must not touch the tree: the refreshed content differs whenever the
        # overlay or media path has moved, so a dry run could silently rewrite it.
        write_agents_local_md(
            wt_root,
            overlay,
            parent_repo=parent,
            media_path=media_dir_for_config(wt_config),
        )

    rc = ensure_proxy_running(dry_run=dry_run)
    if rc != 0:
        return rc
    return _run_worktree_env_compose(
        config,
        slug=overlay.slug,
        compose_args=["up", "-d"],
        dry_run=dry_run,
    )


def worktree_down(
    config: ProjectConfig,
    *,
    slug: str | None = None,
    dry_run: bool = False,
) -> int:
    """Compose ``down`` for the worktree stack (keeps volumes)."""
    return _run_worktree_env_compose(
        config,
        slug=slug,
        compose_args=["down"],
        dry_run=dry_run,
    )


def worktree_restart(
    config: ProjectConfig,
    *,
    slug: str | None = None,
    services: list[str] | None = None,
    dry_run: bool = False,
) -> int:
    """Compose ``restart`` for the worktree stack (optional service names)."""
    compose_args = ["restart", *(services or [])]
    return _run_worktree_env_compose(
        config,
        slug=slug,
        compose_args=compose_args,
        dry_run=dry_run,
    )


def worktree_logs(
    config: ProjectConfig,
    *,
    slug: str | None = None,
    follow: bool = False,
    services: list[str] | None = None,
    dry_run: bool = False,
) -> int:
    """Compose ``logs`` for the worktree stack."""
    compose_args = ["logs"]
    if follow:
        compose_args.append("-f")
    compose_args.extend(services or [])
    return _run_worktree_env_compose(
        config,
        slug=slug,
        compose_args=compose_args,
        dry_run=dry_run,
    )


def worktree_status(
    config: ProjectConfig,
    *,
    slug: str | None = None,
) -> int:
    """Print overlay details, stack status, and optional compose ps summary."""
    try:
        wt_root, overlay, wt_config = _resolve_worktree(config, slug)
    except WorktreeOverlayError as exc:
        print(f"dk worktree status: {exc}", file=sys.stderr)
        return 1

    status = worktree_stack_status(wt_config, overlay)
    print(f"path: {wt_root}")
    print(f"slug: {overlay.slug}")
    print(f"base_env: {overlay.base_env}")
    print(f"compose_project_name: {overlay.compose_project_name}")
    print(f"site_origin: {overlay.site_origin}")
    print(f"branch: {overlay.branch or '-'}")
    print(f"stack: {status}")

    resolved = _worktree_compose_env(wt_config, overlay)
    if resolved is None:
        return 0
    compose_abs, env_r = resolved
    try:
        _compose(compose_abs, "ps", check=False, env_add=env_r)
    except Exception:
        pass
    return 0


def worktree_context(
    config: ProjectConfig,
    *,
    slug: str | None = None,
    as_json: bool = False,
) -> int:
    """Print worktree identity for humans or agents (``--json``)."""
    # Under --json, stdout must be only the JSON: send the helpers' ``$ git …`` echoes to stderr.
    quiet = contextlib.redirect_stdout(sys.stderr) if as_json else contextlib.nullcontext()
    with quiet:
        try:
            payload = _worktree_context_payload(config, slug)
        except WorktreeOverlayError as exc:
            print(f"dk worktree context: {exc}", file=sys.stderr)
            return 1

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"slug: {payload['slug']}")
    print(f"worktree: {payload['worktree']}")
    print(f"parent_repo_root: {payload['parent_repo_root']}")
    print(f"branch: {payload['branch'] or '-'}")
    print(f"head_commit: {payload['head_commit'] or '-'}")
    print(f"base_ref: {payload['base_ref'] or '-'}")
    print(f"base_commit: {payload['base_commit'] or '-'}")
    print(f"base_env: {payload['base_env']}")
    print(f"compose_project_name: {payload['compose_project_name']}")
    print(f"site_origin: {payload['site_origin']}")
    print(f"status: {payload['status']}")
    print(f"seeded: {payload['seeded']}")
    print(f"agents_local: {Path(payload['worktree']) / AGENTS_LOCAL_NAME}")
    return 0


def _worktree_context_payload(config: ProjectConfig, slug: str | None) -> dict[str, Any]:
    """Resolve the worktree, refresh ``AGENTS.local.md``, and gather ``dk worktree context``."""
    wt_root, overlay, wt_config = _resolve_worktree(config, slug)

    parent = (
        Path(overlay.parent_repo_root).resolve()
        if overlay.parent_repo_root
        else resolve_parent_repo_root(config)
    )
    write_agents_local_md(
        wt_root,
        overlay,
        parent_repo=parent,
        media_path=media_dir_for_config(wt_config),
    )

    branch, head_commit = _git_head_info(wt_root)
    payload: dict[str, Any] = {
        "slug": overlay.slug,
        "worktree": str(wt_root.resolve()),
        "parent_repo_root": str(parent.resolve()),
        "branch": branch or overlay.branch,
        "head_commit": head_commit,
        "base_ref": overlay.base_ref,
        "base_commit": overlay.base_commit,
        "base_env": overlay.base_env,
        "compose_project_name": overlay.compose_project_name,
        "site_origin": overlay.site_origin,
        "status": worktree_stack_status(wt_config, overlay),
        "seeded": _media_seeded(wt_config, overlay),
    }
    return payload


def list_worktree_overlays(main_root: Path) -> list[tuple[Path, WorktreeOverlay | None]]:
    """Scan ``.worktrees/*/`` under ``main_root``."""
    base = worktrees_dir(main_root)
    if not base.is_dir():
        return []
    out: list[tuple[Path, WorktreeOverlay | None]] = []
    for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        try:
            overlay = load_worktree_overlay(child)
        except WorktreeOverlayError:
            overlay = None
        out.append((child, overlay))
    return out


def worktree_create(
    config: ProjectConfig,
    *,
    slug: str,
    branch: str | None = None,
    base_branch: str | None = None,
    base_env: str = DEFAULT_BASE_ENV,
    dry_run: bool = False,
    init_submodules: bool = True,
    shallow_submodules: bool = True,
    seed: bool = True,
    bring_up: bool = False,
) -> int:
    """Create a git worktree under ``.worktrees/<slug>`` with a worktree overlay.

    By default seeds DB + media from the main checkout's ``base_env``
    (pass ``seed=False`` / ``--no-seed`` to skip).
    """
    if load_worktree_overlay(config.repo_root) is not None:
        print(
            "dk worktree create: refuse to nest — already inside a worktree "
            f"({worktree_marker_path(config.repo_root)}).",
            file=sys.stderr,
        )
        return 1

    try:
        clean = sanitize_worktree_slug(slug)
    except WorktreeOverlayError as exc:
        print(f"dk worktree create: {exc}", file=sys.stderr)
        return 1

    main_root = config.repo_root.resolve()
    dest = worktrees_dir(main_root) / clean
    if dest.exists():
        print(f"dk worktree create: path already exists: {dest}", file=sys.stderr)
        return 1

    for path, existing in list_worktree_overlays(main_root):
        if existing and existing.slug == clean:
            print(
                f"dk worktree create: slug {clean!r} already used at {path}",
                file=sys.stderr,
            )
            return 1

    # Collision on compose project / site_origin
    try:
        candidate = build_worktree_overlay(
            config,
            slug=clean,
            base_env=base_env,
            parent_repo_root=main_root,
            branch=branch,
        )
    except WorktreeOverlayError as exc:
        print(f"dk worktree create: {exc}", file=sys.stderr)
        return 1

    for path, existing in list_worktree_overlays(main_root):
        if existing is None:
            continue
        if existing.compose_project_name == candidate.compose_project_name:
            print(
                f"dk worktree create: compose project "
                f"{candidate.compose_project_name!r} already used at {path}",
                file=sys.stderr,
            )
            return 1
        if existing.site_origin == candidate.site_origin:
            print(
                f"dk worktree create: site_origin {candidate.site_origin!r} "
                f"already used at {path}",
                file=sys.stderr,
            )
            return 1

    ensure_worktree_gitignore(main_root, dry_run=dry_run)

    start_ref = (base_branch or "HEAD").strip() or "HEAD"
    branch_name = (branch or _default_branch_name(clean)).strip()

    if not dry_run:
        worktrees_dir(main_root).mkdir(parents=True, exist_ok=True)

    # Record what a new branch was cut from; an existing branch ignores start_ref.
    base_ref: str | None = None
    base_commit: str | None = None
    if _branch_exists(main_root, branch_name):
        git_args = ["worktree", "add", str(dest), branch_name]
    else:
        git_args = ["worktree", "add", "-b", branch_name, str(dest), start_ref]
        base_ref, base_commit = _resolve_start_ref(main_root, start_ref)

    result = _git(*git_args, cwd=main_root, check=False, dry_run=dry_run)
    if result.returncode != 0:
        print("dk worktree create: git worktree add failed.", file=sys.stderr)
        return result.returncode or 1

    if init_submodules:
        # dry-run: worktree path may not exist; use main's .gitmodules as the signal
        probe = dest if (not dry_run and dest.is_dir()) else main_root
        if _has_gitmodules(probe):
            if dry_run:
                for path in _gitmodules_paths(probe) or ["<all>"]:
                    ref = main_root / path
                    ref_note = (
                        f" --reference {ref.resolve()}"
                        if path != "<all>" and _is_usable_submodule_reference(ref)
                        else ""
                    )
                    depth_note = " --depth 1" if shallow_submodules else ""
                    print(
                        f"dry-run: git submodule update --init --recursive"
                        f"{depth_note}{ref_note}"
                        f"{'' if path == '<all>' else f' -- {path}'} "
                        f"(cwd={dest})",
                        file=sys.stderr,
                    )
            else:
                rc = _init_worktree_submodules(
                    dest,
                    main_root=main_root,
                    shallow=shallow_submodules,
                    dry_run=False,
                )
                if rc != 0:
                    return rc

    overlay = build_worktree_overlay(
        config,
        slug=clean,
        base_env=base_env,
        parent_repo_root=main_root,
        branch=branch_name,
        base_ref=base_ref,
        base_commit=base_commit,
    )
    if dry_run:
        print(f"dry-run: write {dest / WORKTREE_MARKER_NAME}", file=sys.stderr)
        print(f"dry-run: overlay={overlay.to_dict()!r}", file=sys.stderr)
        print(f"dry-run: write {dest / AGENTS_LOCAL_NAME}", file=sys.stderr)
    else:
        write_worktree_overlay(dest, overlay)
        try:
            wt_config_for_agents = load_project_config(dest)
            media_path = media_dir_for_config(wt_config_for_agents)
        except Exception:
            media_path = dest / "media"
        write_agents_local_md(
            dest,
            overlay,
            parent_repo=main_root,
            media_path=media_path,
        )

    bring_up_ok = False
    if seed:
        if dry_run:
            print(
                f"dry-run: seed DB + media from main {base_env} → worktree {clean!r}",
                file=sys.stderr,
            )
        else:
            rc = worktree_seed(
                config,
                slug=clean,
                do_db=True,
                do_media=True,
                dry_run=False,
                yes=True,
            )
            if rc != 0:
                print(
                    f"Worktree {clean!r} was created but seed failed.",
                    file=sys.stderr,
                )
                print(
                    f"  Retry: uv run dk worktree seed {clean} -y",
                    file=sys.stderr,
                )
                return rc

    if bring_up:
        if dry_run:
            print(
                f"dry-run: worktree up {clean!r} (proxy + {base_env} up -d)",
                file=sys.stderr,
            )
        else:
            rc = worktree_up(config, slug=clean, dry_run=False)
            if rc != 0:
                print(
                    f"Worktree {clean!r} was created but bring-up failed.",
                    file=sys.stderr,
                )
                print(
                    f"  Retry: uv run dk worktree up {clean}",
                    file=sys.stderr,
                )
                return rc
            bring_up_ok = True

    rel = dest.relative_to(main_root) if dest.is_relative_to(main_root) else dest
    print(f"Worktree ready: {rel}", file=sys.stderr)
    print(f"  branch: {branch_name}", file=sys.stderr)
    print(f"  compose: {overlay.compose_project_name}", file=sys.stderr)
    print(f"  site: {overlay.site_origin}", file=sys.stderr)
    print("Next (from the main checkout — no cd / direnv needed):", file=sys.stderr)
    if not seed:
        print(
            f"  uv run dk worktree seed {clean} -y   # copy DB + media from main dev",
            file=sys.stderr,
        )
    if not bring_up_ok:
        print(f"  uv run dk worktree up {clean}", file=sys.stderr)
    print(
        f"  uv run dk --worktree {clean} {base_env} manage migrate   # after first up",
        file=sys.stderr,
    )
    print(
        f"Optional: cd {rel} && uv sync  (for editing/running code in that tree); "
        "File → Add Folder to Workspace in Cursor/VS Code.",
        file=sys.stderr,
    )
    return 0


def worktree_list(config: ProjectConfig) -> int:
    main_root = resolve_parent_repo_root(config)
    entries = list_worktree_overlays(main_root)
    if not entries:
        print(f"No worktrees under {worktrees_dir(main_root)}", file=sys.stderr)
        return 0
    for path, overlay in entries:
        if overlay is None:
            print(f"{path.name}\t{path}\t(no {WORKTREE_MARKER_NAME})")
            continue
        status = "unknown"
        try:
            wt_config = load_project_config(path)
            status = worktree_stack_status(wt_config, overlay)
        except Exception:
            pass
        print(
            f"{overlay.slug}\t{path}\t"
            f"branch={overlay.branch or '-'}\t"
            f"compose={overlay.compose_project_name}\t"
            f"site={overlay.site_origin}\t"
            f"stack={status}"
        )
    return 0


def worktree_info(config: ProjectConfig, *, slug: str | None = None) -> int:
    main_root = resolve_parent_repo_root(config)
    if slug:
        try:
            clean = sanitize_worktree_slug(slug)
        except WorktreeOverlayError as exc:
            print(f"dk worktree info: {exc}", file=sys.stderr)
            return 1
        path = worktrees_dir(main_root) / clean
        if not path.is_dir():
            print(f"dk worktree info: unknown worktree {clean!r} ({path})", file=sys.stderr)
            return 1
    else:
        path = config.repo_root
        if load_worktree_overlay(path) is None:
            # Maybe cwd is main — show hint
            print(
                "dk worktree info: not inside a worktree "
                f"(no {WORKTREE_MARKER_NAME}). Pass a slug or cd into .worktrees/<slug>.",
                file=sys.stderr,
            )
            return 1

    try:
        overlay = load_worktree_overlay(path)
    except WorktreeOverlayError as exc:
        print(f"dk worktree info: {exc}", file=sys.stderr)
        return 1
    if overlay is None:
        print(f"dk worktree info: missing {WORKTREE_MARKER_NAME} in {path}", file=sys.stderr)
        return 1

    print(f"path: {path}")
    print(f"slug: {overlay.slug}")
    print(f"base_env: {overlay.base_env}")
    print(f"compose_project_name: {overlay.compose_project_name}")
    print(f"site_origin: {overlay.site_origin}")
    print(f"branch: {overlay.branch or '-'}")
    print(f"parent_repo_root: {overlay.parent_repo_root or main_root}")
    try:
        wt_config = load_project_config(path)
        print(f"stack: {worktree_stack_status(wt_config, overlay)}")
    except Exception:
        print("stack: unknown")
    return 0


def _teardown_worktree_stack(
    worktree_root: Path,
    overlay: WorktreeOverlay,
    *,
    remove_volumes: bool,
    dry_run: bool = False,
) -> int:
    """``compose down`` for the remapped project; with ``remove_volumes``, its volumes too.

    Both of ``remove``'s modes come through here, because the difference between them is only
    whether the data goes. Retiring the checkout without stopping the stack is not one of the
    modes: the containers outlive the only thing that could name them.

    With ``remove_volumes``, this also sweeps the ``external:`` volumes. Compose never deletes
    one — it does not own what it did not create — so ``down -v`` alone leaves the worktree's
    PGDATA, media and conf volumes behind for a project name that no longer has a checkout.
    See issue #64.
    """
    label = "dk worktree remove --wipe" if remove_volumes else "dk worktree remove"
    try:
        wt_config = load_project_config(worktree_root)
    except Exception as exc:
        print(f"{label}: cannot load tooling at {worktree_root}: {exc}", file=sys.stderr)
        return 1
    ctx = load_managed_deploy_context(wt_config, overlay.base_env)
    if ctx is None:
        print(
            f"{label}: could not load deploy context "
            f"(is {overlay.base_env!r} configured?).",
            file=sys.stderr,
        )
        return 1
    env_r = resolve_env_with_compose_project(
        ctx.compose_file,
        ctx.env_add,
        config=wt_config,
        dk_env_name=overlay.base_env,
    )
    env_r["COMPOSE_PROJECT_NAME"] = overlay.compose_project_name
    compose_abs = str((wt_config.repo_root / ctx.compose_file).resolve())
    down_args = ["down", "-v", "--remove-orphans"] if remove_volumes else ["down", "--remove-orphans"]
    if dry_run:
        print(
            f"dry-run: docker compose -f {compose_abs} {' '.join(down_args)} "
            f"(COMPOSE_PROJECT_NAME={overlay.compose_project_name})",
            file=sys.stderr,
        )
        if not remove_volumes:
            return 0
        return remove_all_external_stack_volumes(env_r, config=wt_config, dry_run=True)
    result = _compose(
        compose_abs,
        *down_args,
        check=False,
        env_add=env_r,
    )
    if result.returncode != 0:
        return result.returncode
    if not remove_volumes:
        return 0
    return remove_all_external_stack_volumes(env_r, config=wt_config)


def worktree_remove(
    config: ProjectConfig,
    *,
    slug: str,
    wipe: bool = False,
    keep_stack: bool = False,
    dry_run: bool = False,
    yes: bool = False,
) -> int:
    main_root = resolve_parent_repo_root(config)
    try:
        clean = sanitize_worktree_slug(slug)
    except WorktreeOverlayError as exc:
        print(f"dk worktree remove: {exc}", file=sys.stderr)
        return 1

    path = worktrees_dir(main_root) / clean
    if not path.is_dir():
        print(f"dk worktree remove: unknown worktree {clean!r} ({path})", file=sys.stderr)
        return 1

    try:
        overlay = load_worktree_overlay(path)
    except WorktreeOverlayError as exc:
        print(f"dk worktree remove: {exc}", file=sys.stderr)
        return 1

    if not yes and not dry_run and not sys.stdin.isatty():
        print(
            "Refusing worktree remove without a TTY. Pass --yes for non-interactive use.",
            file=sys.stderr,
        )
        return 1
    if not yes and not dry_run and sys.stdin.isatty():
        prompt = f"Type {clean!r} to confirm remove"
        if wipe:
            prompt += " (and wipe Docker volumes)"
        elif keep_stack:
            prompt += " (leaving its stack running)"
        else:
            prompt += " (and stop its stack, keeping volumes)"
        prompt += ": "
        if input(prompt).strip() != clean:
            print("dk worktree remove: cancelled.", file=sys.stderr)
            return 1

    if overlay is not None and not keep_stack:
        rc = _teardown_worktree_stack(path, overlay, remove_volumes=wipe, dry_run=dry_run)
        if rc != 0:
            # Keep the checkout: removing it here would strand whatever the teardown could not
            # clear, which is the silent-orphan failure this command exists to prevent.
            # Every volume was attempted, so the output above is the complete picture.
            print(
                f"dk worktree remove: teardown incomplete; leaving {path} in place. "
                "Clear the reported containers or volumes (something may still be using one), "
                "then re-run — anything already removed is skipped. "
                "If the Docker daemon is simply not running, --keep-stack removes the "
                "checkout and leaves the stack for later.",
                file=sys.stderr,
            )
            return rc

    result = _git(
        "worktree",
        "remove",
        "--force",
        str(path),
        cwd=main_root,
        check=False,
        dry_run=dry_run,
    )
    if result.returncode != 0:
        # Fallback: path may already be gone from git's view
        if path.exists() and not dry_run:
            print(
                "dk worktree remove: git worktree remove failed; "
                f"leaving {path} in place.",
                file=sys.stderr,
            )
            return result.returncode or 1
    # Under --dry-run nothing was removed, and the swlt wave procedure reads this output to
    # decide whether to run the real --wipe. Claiming a removal that did not happen is the
    # one thing a preview must not do (#108).
    verb = "Would remove" if dry_run else "Removed"
    print(f"{verb} worktree {clean!r} ({path})", file=sys.stderr)
    if overlay is not None and not wipe:
        # The checkout is gone, so nothing will name this project again. Say what survived and
        # how to reclaim it, rather than leaving it to be found by `docker volume ls` months
        # later — that discovery gap is the whole complaint behind issue #64.
        project = overlay.compose_project_name
        what = "Its stack is still running, with its volumes" if keep_stack else "Its volumes are kept"
        print(
            f"{what} (use --wipe to remove them). To reclaim later:\n"
            f"  docker volume rm $(docker volume ls -q "
            f"--filter label=com.docker.compose.project={project})",
            file=sys.stderr,
        )
    return 0


def _ensure_destination_media_volume(
    dst_media: MediaStorage,
    dst_r: dict[str, str],
    *,
    dest_config: ProjectConfig,
    dst_ctx: Any,
    base_env: str,
) -> None:
    """Pre-create the destination volume with Compose labels.

    ``docker run -v name:/data`` would create it unlabelled, and the next ``compose up``
    then warns that the volume was not created by Compose. This is the same ensure the
    deploy path runs, so it also covers ``external:`` volumes and host-storage binds.
    """
    if dst_media.kind is not MediaStorageKind.VOLUME:
        return
    from catalpa_tooling.dk_transfer import _docker_volume_exists

    if _docker_volume_exists(dst_media.location, dst_r):
        return
    from catalpa_tooling.env_handlers import _ensure_stack_volumes

    rc = _ensure_stack_volumes(
        dest_config,
        base_env,
        dst_ctx.info,
        dst_r,
        dst_ctx.storage_volumes,
    )
    if rc != 0:
        # Not fatal: the transfer still works, the volume is just unlabelled until the
        # next `ensure_volumes`. Say so rather than failing a seed that would succeed.
        print(
            f"worktree seed: could not pre-create {dst_media.location!r} with compose "
            "labels; continuing (run `dk <env> ensure_volumes` if compose complains).",
            file=sys.stderr,
        )


def _seed_media(
    src_r: dict[str, str],
    dst_r: dict[str, str],
    *,
    src_media: MediaStorage,
    dst_media: MediaStorage,
    dest_config: ProjectConfig,
    dest_overlay: WorktreeOverlay,
    dst_ctx: Any,
    base_env: str,
    media_dir: Path,
) -> tuple[int, str]:
    """Copy the source stack's media storage onto the worktree's. Returns ``(rc, outcome)``."""
    del dest_overlay  # identity is already in the header line
    has_content = media_storage_has_content(src_media, src_r)
    if has_content is False:
        return 0, f"skipped, source {src_media.describe()} is empty or missing"
    if has_content is None:
        print(
            f"worktree seed: could not read source {src_media.describe()}; "
            "attempting the copy anyway.",
            file=sys.stderr,
        )

    _ensure_destination_media_volume(
        dst_media,
        dst_r,
        dest_config=dest_config,
        dst_ctx=dst_ctx,
        base_env=base_env,
    )

    media_dir.mkdir(parents=True, exist_ok=True)
    rc = run_transfer_media(
        src_r,
        dst_r,
        src_media=src_media,
        dst_media=dst_media,
        media_dir=media_dir,
        config=dest_config,
    )
    if rc != 0:
        return rc, f"failed copying {src_media.describe()} → {dst_media.describe()}"
    return 0, f"copied {src_media.describe()} → {dst_media.describe()}"


def _seed_volume_keys(config: ProjectConfig) -> tuple[str, ...]:
    """Volumes to seed beyond the media storage: restic's extra volumes, then ``seed_volumes``.

    Anything worth backing up holds files the database refers to, so it is worth seeding too.
    """
    restic_extras = config.ops_optional.restic.extra_volumes if config.ops_optional else ()
    return tuple(dict.fromkeys((*restic_extras, *config.worktree.seed_volumes)))


def _plan_seed_volumes(
    seed_volumes: tuple[str, ...],
    *,
    src_media: MediaStorage,
    dst_media: MediaStorage,
    src_vols: StackVolumes,
    dst_vols: StackVolumes,
) -> tuple[list[tuple[str, MediaStorage | None, MediaStorage | None]], list[str]]:
    """Pair each ``worktree.seed_volumes`` key with its source and destination storage.

    Returns ``(planned, unlisted)``. A planned entry has ``None`` storages when either stack
    does not declare the key, so the caller reports it as skipped rather than dropping it.
    ``unlisted`` names the volumes the web service mounts that nothing will seed — the silent
    gap behind issue #103.
    """
    media_locations = {src_media.location, dst_media.location}
    planned: list[tuple[str, MediaStorage | None, MediaStorage | None]] = []
    for key in seed_volumes:
        src_name, dst_name = src_vols.names.get(key), dst_vols.names.get(key)
        if src_name in media_locations or dst_name in media_locations:
            continue  # already the media storage
        if src_name is None or dst_name is None:
            planned.append((key, None, None))
            continue
        planned.append(
            (
                key,
                MediaStorage(MediaStorageKind.VOLUME, src_name),
                MediaStorage(MediaStorageKind.VOLUME, dst_name),
            )
        )
    unlisted = [
        key
        for key in dst_vols.web_mounts
        if key not in seed_volumes and dst_vols.names[key] not in media_locations
    ]
    return planned, unlisted


def _seed_database(
    src_compose: str,
    src_r: dict[str, str],
    dst_compose: str,
    dst_r: dict[str, str],
    *,
    src_config: ProjectConfig,
    dest_config: ProjectConfig,
    base_env: str,
    dump_path: Path,
) -> tuple[int, str]:
    """``pg_dump`` the source stack and restore it into the worktree's. ``(rc, outcome)``."""
    rc = ensure_db_service_running(
        src_compose,
        src_r,
        config=src_config,
        dk_env_name=base_env,
    )
    if rc != 0:
        print(
            f"worktree seed: could not ensure source `{base_env}` db service is running.",
            file=sys.stderr,
        )
        return rc, "failed, source db would not start"
    rc = ensure_db_service_running(
        dst_compose,
        dst_r,
        config=dest_config,
        dk_env_name=base_env,
    )
    if rc != 0:
        print(
            f"worktree seed: could not ensure destination `{base_env}` db service is running.",
            file=sys.stderr,
        )
        return rc, "failed, destination db would not start"

    print("worktree seed: pg_dump (source) …", file=sys.stderr)
    rc = run_pg_dump_to_file(src_compose, src_r, dump_path)
    if rc != 0:
        return rc, "failed during pg_dump"
    print("worktree seed: drop + recreate app database (destination) …", file=sys.stderr)
    rc = run_drop_create_app_database(
        dst_compose,
        dst_r,
        postgis=dest_config.native.reset_db.postgis,
    )
    if rc != 0:
        return rc, "failed recreating the destination database"
    print("worktree seed: pg_restore (destination) …", file=sys.stderr)
    rc = run_pg_restore(
        dst_compose,
        dst_r,
        compose_pg_restore_extras_for_config(
            dest_config,
            ["--file", str(dump_path)],
        ),
        config=dest_config,
    )
    if rc != 0:
        return rc, "failed during pg_restore"
    return 0, "restored"


def worktree_seed(
    config: ProjectConfig,
    *,
    slug: str | None = None,
    do_db: bool = True,
    do_media: bool = True,
    from_path: Path | None = None,
    dry_run: bool = False,
    yes: bool = False,
) -> int:
    """Copy DB + media from the main checkout's ``dev`` stack into a worktree.

    From the main checkout: ``dk worktree seed <slug>``.
    From inside a worktree (or with ``dk --worktree <slug>``): ``dk worktree seed``.

    Media is copied between the two stacks' **media storages** — the named volume a Docker
    project serves from, or its host bind — not between host directories, which never reached
    the stack. The whole storage is copied, Wagtail originals and renditions together, from the
    same source stack as the DB dump, so image rows and the files they point at stay consistent.
    The copy replaces whatever the destination held.

    Every step reports what it actually did, and the final line summarises those outcomes. A
    source with no media is legitimate: the step is recorded as skipped and the exit code stays 0.
    """
    dest_config = config
    if slug:
        try:
            dest_root = resolve_worktree_root(config.repo_root, slug)
        except WorktreeOverlayError as exc:
            print(f"dk worktree seed: {exc}", file=sys.stderr)
            return 1
        try:
            dest_config = load_project_config(dest_root)
        except Exception as exc:
            print(f"dk worktree seed: cannot load tooling at {dest_root}: {exc}", file=sys.stderr)
            return 1

    dest_overlay = load_worktree_overlay(dest_config.repo_root)
    if dest_overlay is None:
        print(
            f"dk worktree seed: no {WORKTREE_MARKER_NAME} in {dest_config.repo_root}. "
            "Pass a slug (dk worktree seed <slug>) or run from inside a worktree "
            "(or dk --worktree <slug> worktree seed).",
            file=sys.stderr,
        )
        return 1

    if from_path is not None:
        src_root = from_path.expanduser().resolve()
    elif dest_overlay.parent_repo_root:
        src_root = Path(dest_overlay.parent_repo_root).resolve()
    else:
        src_root = resolve_parent_repo_root(dest_config)

    if src_root.resolve() == dest_config.repo_root.resolve():
        print(
            "dk worktree seed: source and destination are the same checkout. "
            "Pass --from <main-repo> or set parent_repo_root in the marker.",
            file=sys.stderr,
        )
        return 1

    try:
        src_config = load_project_config(src_root)
    except Exception as exc:
        print(f"dk worktree seed: cannot load source tooling at {src_root}: {exc}", file=sys.stderr)
        return 1

    base_env = dest_overlay.base_env
    print(
        f"worktree seed: {src_root} ({base_env}) → {dest_config.repo_root} "
        f"(compose={dest_overlay.compose_project_name})",
        file=sys.stderr,
    )
    print(f"  steps: db={do_db} media={do_media}", file=sys.stderr)

    # Both steps need the resolved compose context, so load it once up front: the media step
    # copies between the stacks the DB step dumps and restores, not between host trees.
    src_ctx = load_managed_deploy_context(src_config, base_env, apply_worktree=False)
    if src_ctx is None:
        print(
            f"worktree seed: could not load source deploy context for `{base_env}`.",
            file=sys.stderr,
        )
        return 1
    dst_ctx = load_managed_deploy_context(dest_config, base_env, apply_worktree=True)
    if dst_ctx is None:
        print(
            f"worktree seed: could not load destination deploy context for `{base_env}`.",
            file=sys.stderr,
        )
        return 1

    src_r = resolve_env_with_compose_project(
        src_ctx.compose_file,
        src_ctx.env_add,
        config=src_config,
        dk_env_name=base_env,
    )
    dst_r = resolve_env_with_compose_project(
        dst_ctx.compose_file,
        dst_ctx.env_add,
        config=dest_config,
        dk_env_name=base_env,
    )
    dst_r["COMPOSE_PROJECT_NAME"] = dest_overlay.compose_project_name
    src_compose = str((src_config.repo_root / src_ctx.compose_file).resolve())
    dst_compose = str((dest_config.repo_root / dst_ctx.compose_file).resolve())

    src_media: MediaStorage | None = None
    dst_media: MediaStorage | None = None
    extra_volumes: list[tuple[str, MediaStorage | None, MediaStorage | None]] = []
    if do_media:
        try:
            src_media = resolve_media_storage(
                compose_file=src_compose,
                env=src_r,
                config=src_config,
            )
            dst_media = resolve_media_storage(
                compose_file=dst_compose,
                env=dst_r,
                config=dest_config,
            )
        except MediaStorageError as exc:
            print(f"worktree seed: {exc}", file=sys.stderr)
            return 1
        extra_volumes, unlisted = _plan_seed_volumes(
            _seed_volume_keys(dest_config),
            src_media=src_media,
            dst_media=dst_media,
            src_vols=resolve_stack_volumes(compose_file=src_compose, env=src_r, config=src_config),
            dst_vols=resolve_stack_volumes(
                compose_file=dst_compose, env=dst_r, config=dest_config
            ),
        )
        for key in unlisted:
            print(
                f"worktree seed: volume {key} is mounted by the web service but not seeded. "
                "If it holds files the database refers to, list it under "
                "worktree.seed_volumes in tooling.yaml.",
                file=sys.stderr,
            )

    if dry_run:
        if do_media:
            assert src_media is not None and dst_media is not None
            print(
                f"dry-run: media {src_media.describe()} → {dst_media.describe()}",
                file=sys.stderr,
            )
            for key, src_vol, dst_vol in extra_volumes:
                if src_vol is None or dst_vol is None:
                    print(
                        f"dry-run: volume {key} skipped, not declared in both stacks' compose files",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"dry-run: volume {key} {src_vol.describe()} → {dst_vol.describe()}",
                        file=sys.stderr,
                    )
        if do_db:
            print(
                f"dry-run: pg_dump from main {base_env} → "
                f"pg_restore into {dest_overlay.compose_project_name}",
                file=sys.stderr,
            )
            return run_post_db_restore_manage_commands(
                dest_config,
                compose_file=dst_compose,
                env_add=dst_r,
                env_name=base_env,
                dry_run=True,
            )
        return 0

    if not yes and not sys.stdin.isatty():
        print(
            "Refusing worktree seed without a TTY. Pass --yes for non-interactive use.",
            file=sys.stderr,
        )
        return 1
    if not yes and sys.stdin.isatty():
        typed = input(
            f"Type {dest_overlay.slug!r} to confirm overwriting this worktree DB/media: "
        ).strip()
        if typed != dest_overlay.slug:
            print("worktree seed: cancelled.", file=sys.stderr)
            return 1

    outcomes: dict[str, str] = {
        "db": "not requested",
        "media": "not requested",
    }

    def _summary() -> str:
        return "; ".join(f"{step}: {outcome}" for step, outcome in outcomes.items())

    parent = dest_config.repo_root / dest_config.ops.transfer_workdir
    parent.mkdir(parents=True, exist_ok=True)
    session = Path(
        tempfile.mkdtemp(
            prefix=f"worktree_seed_{dest_overlay.slug}_",
            dir=str(parent),
        )
    )
    try:
        if do_media:
            assert src_media is not None and dst_media is not None
            rc, outcomes["media"] = _seed_media(
                src_r,
                dst_r,
                src_media=src_media,
                dst_media=dst_media,
                dest_config=dest_config,
                dest_overlay=dest_overlay,
                dst_ctx=dst_ctx,
                base_env=base_env,
                media_dir=session / "media",
            )
            if rc != 0:
                return rc
            print(f"worktree seed: media {outcomes['media']}.", file=sys.stderr)

            for key, src_vol, dst_vol in extra_volumes:
                step = f"volume {key}"
                if src_vol is None or dst_vol is None:
                    outcomes[step] = "skipped, not declared in both stacks' compose files"
                else:
                    rc, outcomes[step] = _seed_media(
                        src_r,
                        dst_r,
                        src_media=src_vol,
                        dst_media=dst_vol,
                        dest_config=dest_config,
                        dest_overlay=dest_overlay,
                        dst_ctx=dst_ctx,
                        base_env=base_env,
                        media_dir=session / f"volume_{key}",
                    )
                    if rc != 0:
                        return rc
                print(f"worktree seed: {step} {outcomes[step]}.", file=sys.stderr)

        if do_db:
            rc, outcomes["db"] = _seed_database(
                src_compose,
                src_r,
                dst_compose,
                dst_r,
                src_config=src_config,
                dest_config=dest_config,
                base_env=base_env,
                dump_path=session / "pg.dump",
            )
            if rc != 0:
                return rc
            print(f"worktree seed: database {outcomes['db']}.", file=sys.stderr)

            # The copied rows still describe the main checkout's stack (site hostnames and
            # the like); the project's restore hooks rewrite them for this worktree.
            if post_db_restore_applies(dest_config, base_env):
                rc = run_post_db_restore_manage_commands(
                    dest_config,
                    compose_file=dst_compose,
                    env_add=dst_r,
                    env_name=base_env,
                )
                if rc != 0:
                    outcomes["db"] = "restored, post_db_restore hooks failed"
                    print(f"worktree seed: failed ({_summary()}).", file=sys.stderr)
                    print(
                        f"  Retry: uv run dk worktree seed {dest_overlay.slug} --db -y",
                        file=sys.stderr,
                    )
                    return rc
                outcomes["db"] = "restored, post_db_restore hooks ran"
    finally:
        shutil.rmtree(session, ignore_errors=True)

    print(f"worktree seed: complete ({_summary()}).", file=sys.stderr)
    return 0
