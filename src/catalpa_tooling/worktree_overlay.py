"""Gitignored worktree overlay (``.catalpa-worktree.yaml``) for isolated ``dk dev`` stacks."""

from __future__ import annotations

import copy
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.restic_files import _sanitize_dk_env_for_compose_project_suffix
from catalpa_tooling.site_origin import (
    normalize_site_origin_entry,
    project_slug_from_config,
    role_site_origin_from_primary,
)

WORKTREE_MARKER_NAME = ".catalpa-worktree.yaml"
WORKTREES_DIRNAME = ".worktrees"
DEFAULT_BASE_ENV = "dev"
WORKTREE_MARKER_VERSION = 1

# Keys in info.env that pin origins / compose identity; strip so remapped site_origin wins.
_INFO_ENV_KEYS_TO_STRIP = frozenset(
    {
        "compose_project_name",
        "site_origin",
        "domain",
        "bero_origin",
        "django_origin",
        "metabase_origin",
        "metabase_site_origin",
        "caddy_site_address",
        "caddy_django_site_address",
        "caddy_metabase_site_address",
        "wagtailadmin_base_url",
    }
)


class WorktreeOverlayError(ValueError):
    """Invalid worktree marker or slug."""


@dataclass(frozen=True)
class WorktreeOverlay:
    """Parsed ``.catalpa-worktree.yaml`` at a worktree root."""

    version: int
    slug: str
    base_env: str
    compose_project_name: str
    site_origin: str
    parent_repo_root: str | None = None
    branch: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": self.version,
            "slug": self.slug,
            "base_env": self.base_env,
            "compose_project_name": self.compose_project_name,
            "site_origin": self.site_origin,
        }
        if self.parent_repo_root:
            out["parent_repo_root"] = self.parent_repo_root
        if self.branch:
            out["branch"] = self.branch
        return out


def worktree_marker_path(repo_root: Path) -> Path:
    return Path(repo_root) / WORKTREE_MARKER_NAME


def worktrees_dir(repo_root: Path) -> Path:
    return Path(repo_root) / WORKTREES_DIRNAME


def sanitize_worktree_slug(slug: str) -> str:
    """Compose-safe slug: lowercase ``[a-z0-9_]+`` (hyphens → underscores)."""
    s = (slug or "").strip().lower().replace("-", "_")
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    if not s:
        raise WorktreeOverlayError(f"invalid worktree slug: {slug!r}")
    if len(s) > 40:
        s = s[:40].rstrip("_")
    if not s or s[0].isdigit():
        raise WorktreeOverlayError(
            f"worktree slug must start with a letter after sanitization: {slug!r} → {s!r}"
        )
    return s


def dns_label_from_slug(slug: str) -> str:
    """Hostname label from sanitized slug (underscores → hyphens)."""
    return sanitize_worktree_slug(slug).replace("_", "-")


def derive_worktree_compose_project(
    config: ProjectConfig,
    *,
    base_env: str,
    slug: str,
) -> str:
    """``{compose_project_default}_{base_env}_{slug}``."""
    default = config.stack.compose_project_default
    env_sfx = _sanitize_dk_env_for_compose_project_suffix(base_env)
    slug_sfx = sanitize_worktree_slug(slug)
    return f"{default}_{env_sfx}_{slug_sfx}"


def derive_worktree_site_origin(
    config: ProjectConfig,
    *,
    base_env: str,
    slug: str,
) -> str:
    """``https://{project}-{base_env}-{slug}.{site_origin_base}``."""
    project = project_slug_from_config(config)
    env = (base_env or DEFAULT_BASE_ENV).strip().lower()
    label = dns_label_from_slug(slug)
    base = config.dev.site_origin_base.strip().rstrip(".")
    return normalize_site_origin_entry(f"{project}-{env}-{label}.{base}")


def build_worktree_overlay(
    config: ProjectConfig,
    *,
    slug: str,
    base_env: str = DEFAULT_BASE_ENV,
    parent_repo_root: Path | None = None,
    branch: str | None = None,
) -> WorktreeOverlay:
    """Derive a new overlay from project config + slug."""
    clean = sanitize_worktree_slug(slug)
    env = (base_env or DEFAULT_BASE_ENV).strip().lower() or DEFAULT_BASE_ENV
    return WorktreeOverlay(
        version=WORKTREE_MARKER_VERSION,
        slug=clean,
        base_env=env,
        compose_project_name=derive_worktree_compose_project(
            config, base_env=env, slug=clean
        ),
        site_origin=derive_worktree_site_origin(config, base_env=env, slug=clean),
        parent_repo_root=str(parent_repo_root.resolve()) if parent_repo_root else None,
        branch=branch.strip() if branch else None,
    )


def load_worktree_overlay(repo_root: Path) -> WorktreeOverlay | None:
    """Load marker from ``repo_root``, or None if missing."""
    path = worktree_marker_path(repo_root)
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise WorktreeOverlayError(f"{path}: expected a mapping")
    try:
        version = int(raw.get("version") or WORKTREE_MARKER_VERSION)
        slug = str(raw.get("slug") or "").strip()
        base_env = str(raw.get("base_env") or DEFAULT_BASE_ENV).strip() or DEFAULT_BASE_ENV
        compose = str(raw.get("compose_project_name") or "").strip()
        origin = str(raw.get("site_origin") or "").strip()
        if not slug or not compose or not origin:
            raise WorktreeOverlayError(
                f"{path}: slug, compose_project_name, and site_origin are required"
            )
        parent = raw.get("parent_repo_root")
        branch = raw.get("branch")
        return WorktreeOverlay(
            version=version,
            slug=sanitize_worktree_slug(slug),
            base_env=base_env,
            compose_project_name=compose,
            site_origin=normalize_site_origin_entry(origin),
            parent_repo_root=str(parent).strip() if parent else None,
            branch=str(branch).strip() if branch else None,
        )
    except (TypeError, ValueError) as exc:
        raise WorktreeOverlayError(f"{path}: {exc}") from exc


def write_worktree_overlay(repo_root: Path, overlay: WorktreeOverlay) -> Path:
    """Write ``.catalpa-worktree.yaml`` under ``repo_root``."""
    path = worktree_marker_path(repo_root)
    path.write_text(
        yaml.safe_dump(overlay.to_dict(), default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _strip_origin_keys_from_info_env(info: dict[str, Any]) -> None:
    env_block = info.get("env")
    if not isinstance(env_block, dict):
        return
    for key in list(env_block.keys()):
        if str(key).strip().lower() in _INFO_ENV_KEYS_TO_STRIP:
            del env_block[key]


def apply_worktree_overlay_to_info(
    info: dict[str, Any],
    overlay: WorktreeOverlay,
    *,
    env_name: str,
) -> dict[str, Any] | None:
    """Return a copy of ``info`` remapped for this worktree, or None if not applicable.

    Skips remapping when ``env_name`` is not the overlay base env, or when ``docker_host``
    targets a remote daemon.
    """
    canonical = env_name.strip()
    if canonical != overlay.base_env:
        return None
    docker_host = str(info.get("docker_host") or "").strip()
    if docker_host.lower().startswith("ssh://"):
        print(
            f"worktree overlay: refusing remap for remote env {env_name!r} "
            f"(docker_host={docker_host!r})",
            file=sys.stderr,
        )
        return None

    out = copy.deepcopy(info)
    out["site_origin"] = overlay.site_origin
    if "domain" in out:
        del out["domain"]
    env_block = out.get("env")
    if not isinstance(env_block, dict):
        env_block = {}
        out["env"] = env_block
    _strip_origin_keys_from_info_env(out)
    env_block = out["env"]
    assert isinstance(env_block, dict)
    env_block["compose_project_name"] = overlay.compose_project_name
    return out


def force_worktree_origin_env(
    env_add: dict[str, str],
    *,
    overlay: WorktreeOverlay,
    info: dict[str, Any],
) -> None:
    """Overwrite origin-related compose env keys after normal info/caddy merge.

    ``apply_caddy_site_addresses`` uses ``setdefault``; tracked ``info.env`` values would
    otherwise win. Worktree identity must always follow the overlay.
    """
    from catalpa_tooling.site_origin import (
        domain_env_from_origins,
        hostnames_from_origins,
        local_proxy_role_names,
    )

    primary = overlay.site_origin
    primary_host = hostnames_from_origins([primary])[0]
    origins = [primary]
    roles = local_proxy_role_names(info)
    for role in roles:
        origins.append(role_site_origin_from_primary(primary, role))

    env_add["COMPOSE_PROJECT_NAME"] = overlay.compose_project_name
    env_add["SITE_ORIGIN"] = primary
    env_add["BERO_ORIGIN"] = primary
    env_add["WAGTAILADMIN_BASE_URL"] = primary
    domain_s = domain_env_from_origins(origins)
    if domain_s:
        env_add["DOMAIN"] = domain_s

    env_add["CADDY_SITE_ADDRESS"] = f"http://{primary_host}"
    if "admin" in roles:
        admin_origin = role_site_origin_from_primary(primary, "admin")
        admin_host = hostnames_from_origins([admin_origin])[0]
        env_add["DJANGO_ORIGIN"] = admin_origin
        env_add["CADDY_DJANGO_SITE_ADDRESS"] = f"http://{admin_host}"
    if "stats" in roles:
        stats_origin = role_site_origin_from_primary(primary, "stats")
        stats_host = hostnames_from_origins([stats_origin])[0]
        env_add["METABASE_ORIGIN"] = stats_origin
        env_add["METABASE_SITE_ORIGIN"] = stats_origin
        env_add["CADDY_METABASE_SITE_ADDRESS"] = f"http://{stats_host}"

    extra_hosts = [hostnames_from_origins([o])[0] for o in origins[1:]]
    if extra_hosts:
        env_add["BERO_EXTRA_ALLOWED_HOSTS"] = ", ".join(extra_hosts)
        env_add["DJANGO_EXTRA_ORIGINS"] = ", ".join(origins[1:])
