"""``dk worktree`` — create/list/info/remove/seed isolated git worktrees."""

from __future__ import annotations

import argparse
import json
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
from catalpa_tooling.pgbackrest_db import (
    compose_pg_restore_extras_for_config,
    ensure_db_service_running,
    run_drop_create_app_database,
    run_pg_dump_to_file,
    run_pg_restore,
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


def _media_dir_seeded(config: ProjectConfig) -> bool:
    """True when the host media tree has at least one file."""
    media = media_dir_for_config(config)
    if not media.is_dir():
        return False
    try:
        next(media.rglob("*"))
        return True
    except StopIteration:
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
    try:
        wt_root, overlay, wt_config = _resolve_worktree(config, slug)
    except WorktreeOverlayError as exc:
        print(f"dk worktree context: {exc}", file=sys.stderr)
        return 1

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
        "base_env": overlay.base_env,
        "compose_project_name": overlay.compose_project_name,
        "site_origin": overlay.site_origin,
        "status": worktree_stack_status(wt_config, overlay),
        "seeded": _media_dir_seeded(wt_config),
    }

    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"slug: {payload['slug']}")
    print(f"worktree: {payload['worktree']}")
    print(f"parent_repo_root: {payload['parent_repo_root']}")
    print(f"branch: {payload['branch'] or '-'}")
    print(f"head_commit: {payload['head_commit'] or '-'}")
    print(f"base_env: {payload['base_env']}")
    print(f"compose_project_name: {payload['compose_project_name']}")
    print(f"site_origin: {payload['site_origin']}")
    print(f"status: {payload['status']}")
    print(f"seeded: {payload['seeded']}")
    print(f"agents_local: {wt_root / AGENTS_LOCAL_NAME}")
    return 0


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

    By default seeds DB + host media from the main checkout's ``base_env``
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

    if _branch_exists(main_root, branch_name):
        git_args = ["worktree", "add", str(dest), branch_name]
    else:
        git_args = ["worktree", "add", "-b", branch_name, str(dest), start_ref]

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


def _wipe_worktree_stack(
    worktree_root: Path,
    overlay: WorktreeOverlay,
    *,
    dry_run: bool = False,
) -> int:
    """``compose down -v`` for the remapped project in the worktree checkout."""
    try:
        wt_config = load_project_config(worktree_root)
    except Exception as exc:
        print(f"dk worktree remove --wipe: cannot load tooling at {worktree_root}: {exc}", file=sys.stderr)
        return 1
    ctx = load_managed_deploy_context(wt_config, overlay.base_env)
    if ctx is None:
        print(
            "dk worktree remove --wipe: could not load deploy context "
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
    if dry_run:
        print(
            f"dry-run: docker compose -f {compose_abs} down -v "
            f"(COMPOSE_PROJECT_NAME={overlay.compose_project_name})",
            file=sys.stderr,
        )
        return 0
    result = _compose(
        compose_abs,
        "down",
        "-v",
        "--remove-orphans",
        check=False,
        env_add=env_r,
    )
    return result.returncode


def worktree_remove(
    config: ProjectConfig,
    *,
    slug: str,
    wipe: bool = False,
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
        prompt += ": "
        if input(prompt).strip() != clean:
            print("dk worktree remove: cancelled.", file=sys.stderr)
            return 1

    if wipe and overlay is not None:
        rc = _wipe_worktree_stack(path, overlay, dry_run=dry_run)
        if rc != 0:
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
    print(f"Removed worktree {clean!r} ({path})", file=sys.stderr)
    return 0


def _copy_host_media(src: Path, dst: Path, *, dry_run: bool = False) -> int:
    if dry_run:
        print(f"dry-run: copy media {src} → {dst}", file=sys.stderr)
        return 0
    if not src.is_dir():
        print(
            f"worktree seed: source media dir missing ({src}); skipping media copy.",
            file=sys.stderr,
        )
        dst.mkdir(parents=True, exist_ok=True)
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    # Prefer rsync when available for incremental copy.
    if shutil.which("rsync"):
        result = run_cmd(
            ["rsync", "-a", "--delete", f"{src}/", f"{dst}/"],
            check=False,
            print_cmd=True,
        )
        return result.returncode
    if dst.exists():
        for child in dst.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return 0


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
    """Copy DB + host media from the main checkout's ``dev`` stack into a worktree.

    From the main checkout: ``dk worktree seed <slug>``.
    From inside a worktree (or with ``dk --worktree <slug>``): ``dk worktree seed``.
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

    if dry_run:
        if do_media:
            print(
                f"dry-run: media {media_dir_for_config(src_config)} → "
                f"{media_dir_for_config(dest_config)}",
                file=sys.stderr,
            )
        if do_db:
            print(
                f"dry-run: pg_dump from main {base_env} → "
                f"pg_restore into {dest_overlay.compose_project_name}",
                file=sys.stderr,
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

    if do_media:
        rc = _copy_host_media(
            media_dir_for_config(src_config),
            media_dir_for_config(dest_config),
            dry_run=False,
        )
        if rc != 0:
            return rc
        print("worktree seed: media copy done.", file=sys.stderr)

    if do_db:
        src_ctx = load_managed_deploy_context(
            src_config, base_env, apply_worktree=False
        )
        if src_ctx is None:
            return 1
        dst_ctx = load_managed_deploy_context(
            dest_config, base_env, apply_worktree=True
        )
        if dst_ctx is None:
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
            return rc
        rc = ensure_db_service_running(
            dst_compose,
            dst_r,
            config=dest_config,
            dk_env_name=base_env,
        )
        if rc != 0:
            print(
                f"worktree seed: could not ensure destination `{base_env}` db service "
                "is running.",
                file=sys.stderr,
            )
            return rc

        parent = dest_config.repo_root / dest_config.ops.transfer_workdir
        parent.mkdir(parents=True, exist_ok=True)
        session = Path(
            tempfile.mkdtemp(
                prefix=f"worktree_seed_{dest_overlay.slug}_",
                dir=str(parent),
            )
        )
        dump_path = session / "pg.dump"
        try:
            print("worktree seed: pg_dump (source) …", file=sys.stderr)
            rc = run_pg_dump_to_file(src_compose, src_r, dump_path)
            if rc != 0:
                return rc
            print("worktree seed: drop + recreate app database (destination) …", file=sys.stderr)
            rc = run_drop_create_app_database(
                dst_compose,
                dst_r,
                postgis=dest_config.native.reset_db.postgis,
            )
            if rc != 0:
                return rc
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
                return rc
            print("worktree seed: database done.", file=sys.stderr)
        finally:
            shutil.rmtree(session, ignore_errors=True)

    print("worktree seed: complete.", file=sys.stderr)
    return 0
