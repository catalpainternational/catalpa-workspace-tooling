"""GHCR package cleanup: resolve retention from project config and delete via GitHub Packages API."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import yaml

from catalpa_tooling.images import _image_registry_from_config, _load_images_config
from catalpa_tooling.managed_deploy_env import _info_image_tag
from catalpa_tooling.remote_deploy import list_deploy_env_names
from catalpa_tooling.run_cmd import run as run_cmd

if TYPE_CHECKING:
    from catalpa_tooling.config import ProjectConfig

_GITHUB_API = "https://api.github.com"
_DEFAULT_KEEP_N_TAGGED = 20
_DEFAULT_OLDER_THAN = "180 days"


@dataclass(frozen=True)
class PackageVersion:
    package: str
    version_id: int
    tags: tuple[str, ...]
    created_at: datetime

    @property
    def is_untagged(self) -> bool:
        return not self.tags


@dataclass(frozen=True)
class VersionToDelete:
    package: str
    version_id: int
    tags: tuple[str, ...]
    created_at: datetime
    reason: str


@dataclass
class GhcrCleanupPlan:
    owner: str
    packages: tuple[str, ...]
    keep_n_tagged: int
    older_than: timedelta
    delete_untagged: bool
    exclude_tags: tuple[str, ...] = field(default_factory=tuple)
    collect_deploy_tags: bool = True


def _parse_registry_owner(registry: str) -> str:
    reg = registry.strip().rstrip("/")
    if reg.startswith("ghcr.io/"):
        path = reg.removeprefix("ghcr.io/").strip("/")
        if not path:
            raise ValueError(f"invalid image_registry (missing owner): {registry!r}")
        return path.split("/")[0]
    raise ValueError(f"image_registry must start with ghcr.io/: {registry!r}")


def _parse_older_than(raw: str) -> timedelta:
    text = str(raw).strip().lower()
    m = re.fullmatch(r"(\d+)\s+(second|seconds|minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)", text)
    if not m:
        raise ValueError(f"invalid older_than interval: {raw!r} (e.g. '180 days')")
    amount = int(m.group(1))
    unit = m.group(2)
    if unit.startswith("second"):
        return timedelta(seconds=amount)
    if unit.startswith("minute"):
        return timedelta(minutes=amount)
    if unit.startswith("hour"):
        return timedelta(hours=amount)
    if unit.startswith("day"):
        return timedelta(days=amount)
    if unit.startswith("week"):
        return timedelta(weeks=amount)
    if unit.startswith("month"):
        return timedelta(days=amount * 30)
    if unit.startswith("year"):
        return timedelta(days=amount * 365)
    raise ValueError(f"unsupported older_than unit in {raw!r}")


def _coerce_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1"):
            return True
        if lowered in ("false", "no", "0"):
            return False
    return bool(value)


def _coerce_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                out.append(s)
        return out
    return []


def _try_sops_credentials_tags(creds_path: Path) -> set[str]:
    """Return deploy image tags from SOPS credentials when decryption works locally."""
    if not creds_path.is_file():
        return set()
    result = run_cmd(
        ["sops", "-d", str(creds_path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if result.returncode != 0:
        return set()
    try:
        creds = yaml.safe_load(result.stdout) or {}
    except yaml.YAMLError:
        return set()
    if not isinstance(creds, dict):
        return set()
    tags: set[str] = set()
    for key in ("tag", "tag_db", "tag_caddy"):
        raw = creds.get(key)
        if raw is None:
            continue
        s = str(raw).strip()
        if s:
            tags.add(s)
    return tags


def collect_deploy_tags(config: ProjectConfig) -> set[str]:
    tags: set[str] = set()
    for env_name in list_deploy_env_names(config.deploy_envs_dir):
        env_dir = config.deploy_envs_dir / env_name
        info_path = env_dir / "info.yaml"
        if info_path.is_file():
            with open(info_path, encoding="utf-8") as f:
                info = yaml.safe_load(f) or {}
            if isinstance(info, dict):
                tag = _info_image_tag(info)
                if tag:
                    tags.add(tag)
        tags.update(_try_sops_credentials_tags(env_dir / "credentials.yaml"))
    return tags


def resolve_ghcr_cleanup_plan(
    config: ProjectConfig,
    *,
    keep_n_tagged: int | None = None,
    older_than: str | None = None,
    delete_untagged: bool | None = None,
    packages: tuple[str, ...] | None = None,
    extra_exclude_tags: tuple[str, ...] | None = None,
) -> GhcrCleanupPlan:
    images_config = _load_images_config(config)
    registry = _image_registry_from_config(images_config, config)
    if not registry:
        raise ValueError(
            f"Set {config.stack.images.registry_key} in {config.paths.deploy.images_config} "
            "before running clean-images"
        )
    owner = _parse_registry_owner(registry)

    cleanup_cfg = images_config.get("ghcr_cleanup") or {}
    if not isinstance(cleanup_cfg, dict):
        cleanup_cfg = {}

    resolved_keep = keep_n_tagged
    if resolved_keep is None:
        raw_keep = cleanup_cfg.get("keep_n_tagged", _DEFAULT_KEEP_N_TAGGED)
        resolved_keep = int(raw_keep)

    resolved_older = older_than
    if resolved_older is None:
        resolved_older = str(cleanup_cfg.get("older_than", _DEFAULT_OLDER_THAN))

    resolved_delete_untagged = delete_untagged
    if resolved_delete_untagged is None:
        resolved_delete_untagged = _coerce_bool(cleanup_cfg.get("delete_untagged"), True)

    collect_deploy = _coerce_bool(cleanup_cfg.get("collect_deploy_tags"), True)

    exclude: set[str] = set(_coerce_str_list(cleanup_cfg.get("extra_exclude_tags")))
    if extra_exclude_tags:
        exclude.update(extra_exclude_tags)
    if collect_deploy:
        exclude.update(collect_deploy_tags(config))

    if packages is None:
        package_list = tuple(config.stack.images.components.values())
    else:
        package_list = packages

    if not package_list:
        raise ValueError("No stack image packages configured in tooling.yaml")

    return GhcrCleanupPlan(
        owner=owner,
        packages=package_list,
        keep_n_tagged=max(0, resolved_keep),
        older_than=_parse_older_than(resolved_older),
        delete_untagged=resolved_delete_untagged,
        exclude_tags=tuple(sorted(exclude)),
        collect_deploy_tags=collect_deploy,
    )


def tag_matches_any(tag: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatchcase(tag, pattern):
            return True
    return False


def version_is_excluded(version: PackageVersion, exclude_tags: tuple[str, ...]) -> bool:
    if not exclude_tags:
        return False
    return any(tag_matches_any(tag, exclude_tags) for tag in version.tags)


def _parse_github_timestamp(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_package_version(package: str, payload: dict[str, Any]) -> PackageVersion | None:
    version_id = payload.get("id")
    if version_id is None:
        return None
    metadata = payload.get("metadata") or {}
    container = metadata.get("container") or {}
    tags_raw = container.get("tags") or []
    tags = tuple(str(t).strip() for t in tags_raw if str(t).strip())
    created_raw = payload.get("created_at") or payload.get("updated_at")
    if not created_raw:
        return None
    return PackageVersion(
        package=package,
        version_id=int(version_id),
        tags=tags,
        created_at=_parse_github_timestamp(str(created_raw)),
    )


def plan_deletions(
    plan: GhcrCleanupPlan,
    versions_by_package: dict[str, list[PackageVersion]],
    *,
    now: datetime | None = None,
) -> list[VersionToDelete]:
    current = now or datetime.now(tz=UTC)
    cutoff = current - plan.older_than
    staged: list[VersionToDelete] = []

    for package in plan.packages:
        versions = versions_by_package.get(package, [])
        for version in versions:
            if version_is_excluded(version, plan.exclude_tags):
                continue
            if version.is_untagged:
                if plan.delete_untagged:
                    staged.append(
                        VersionToDelete(
                            package=package,
                            version_id=version.version_id,
                            tags=version.tags,
                            created_at=version.created_at,
                            reason="untagged",
                        )
                    )
                continue

            if version.created_at > cutoff:
                continue

        old_tagged = [
            v
            for v in versions
            if not v.is_untagged
            and not version_is_excluded(v, plan.exclude_tags)
            and v.created_at <= cutoff
        ]
        old_tagged.sort(key=lambda v: v.created_at, reverse=True)
        for version in old_tagged[plan.keep_n_tagged :]:
            staged.append(
                VersionToDelete(
                    package=package,
                    version_id=version.version_id,
                    tags=version.tags,
                    created_at=version.created_at,
                    reason=f"older than {plan.older_than.days}d, outside keep-n={plan.keep_n_tagged}",
                )
            )

    staged.sort(key=lambda v: (v.package, v.created_at))
    return staged


def resolve_github_token(explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    r = run_cmd(
        ["gh", "auth", "token"],
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode == 0 and (r.stdout or "").strip():
        return (r.stdout or "").strip()
    raise RuntimeError(
        "GitHub token required: set GH_TOKEN or GITHUB_TOKEN, or run `gh auth login` "
        "with delete:packages and read:packages scopes"
    )


def _github_request(
    method: str,
    url: str,
    token: str,
    *,
    data: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "catalpa-workspace-tooling-ghcr-cleanup",
    }
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return resp.status, None
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"message": raw or exc.reason}
        raise RuntimeError(
            f"GitHub API {method} {url} failed ({exc.code}): {payload.get('message', raw)}"
        ) from exc


def list_package_versions(
    owner: str,
    package: str,
    token: str,
    *,
    github_request: Callable[..., tuple[int, Any]] | None = None,
) -> list[PackageVersion]:
    request = github_request or _github_request
    encoded = urllib.parse.quote(package.lower(), safe="")
    versions: list[PackageVersion] = []
    page = 1
    while True:
        query = urllib.parse.urlencode({"per_page": "100", "page": str(page), "state": "active"})
        url = f"{_GITHUB_API}/orgs/{urllib.parse.quote(owner)}/packages/container/{encoded}/versions?{query}"
        _, payload = request("GET", url, token)
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            if not isinstance(item, dict):
                continue
            parsed = _parse_package_version(package, item)
            if parsed is not None:
                versions.append(parsed)
        if len(payload) < 100:
            break
        page += 1
    return versions


def delete_package_version(
    owner: str,
    package: str,
    version_id: int,
    token: str,
    *,
    github_request: Callable[..., tuple[int, Any]] | None = None,
) -> None:
    request = github_request or _github_request
    encoded = urllib.parse.quote(package.lower(), safe="")
    url = f"{_GITHUB_API}/orgs/{urllib.parse.quote(owner)}/packages/container/{encoded}/versions/{version_id}"
    request("DELETE", url, token)


def _format_tags(tags: tuple[str, ...]) -> str:
    if not tags:
        return "(untagged)"
    return ", ".join(tags)


def print_cleanup_summary(plan: GhcrCleanupPlan, staged: list[VersionToDelete], *, dry_run: bool) -> None:
    mode = "dry-run" if dry_run else "apply"
    print(f"GHCR cleanup ({mode})", file=sys.stderr)
    print(f"  owner: {plan.owner}", file=sys.stderr)
    print(f"  packages: {', '.join(plan.packages)}", file=sys.stderr)
    print(f"  keep_n_tagged: {plan.keep_n_tagged}", file=sys.stderr)
    print(f"  older_than: {plan.older_than.days} days", file=sys.stderr)
    print(f"  delete_untagged: {plan.delete_untagged}", file=sys.stderr)
    if plan.exclude_tags:
        print(f"  exclude_tags: {', '.join(plan.exclude_tags)}", file=sys.stderr)
    print(file=sys.stderr)

    if not staged:
        print("Nothing to delete.", file=sys.stderr)
        return

    print(f"{'Would delete' if dry_run else 'Deleting'} {len(staged)} package version(s):", file=sys.stderr)
    for item in staged:
        created = item.created_at.strftime("%Y-%m-%d")
        print(
            f"  {item.package} id={item.version_id} tags={_format_tags(item.tags)} "
            f"created={created} ({item.reason})",
            file=sys.stderr,
        )


def run_cleanup(
    plan: GhcrCleanupPlan,
    *,
    dry_run: bool,
    token: str | None = None,
    list_versions: Callable[..., list[PackageVersion]] | None = None,
    delete_version: Callable[..., None] | None = None,
) -> int:
    resolved_token = resolve_github_token(token)
    list_fn = list_versions or list_package_versions
    delete_fn = delete_version or delete_package_version

    versions_by_package: dict[str, list[PackageVersion]] = {}
    for package in plan.packages:
        versions_by_package[package] = list_fn(plan.owner, package, resolved_token)

    staged = plan_deletions(plan, versions_by_package)
    print_cleanup_summary(plan, staged, dry_run=dry_run)

    if dry_run or not staged:
        return 0

    failures = 0
    for item in staged:
        try:
            delete_fn(plan.owner, item.package, item.version_id, resolved_token)
            print(f"Deleted {item.package} id={item.version_id}", file=sys.stderr)
        except RuntimeError as exc:
            failures += 1
            print(f"Failed to delete {item.package} id={item.version_id}: {exc}", file=sys.stderr)
    return 1 if failures else 0
