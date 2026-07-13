"""JavaScript dependency license scan (pnpm lockfile, Yarn Berry PnP cache, license-checker)."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.compliance.licenses import normalize_license_spdx
from catalpa_tooling.compliance.types import CompliancePackage, ComplianceViolation
from catalpa_tooling.config import ComplianceJavascriptConfig, ProjectConfig
from catalpa_tooling.run_cmd import run as run_cmd

PNPM_LOCKFILE = "pnpm-lock.yaml"
YARN_LOCKFILE = "yarn.lock"
NPM_LOCKFILE = "package-lock.json"

_CACHE_HASH_RE = re.compile(r"-[0-9a-f]{10}$")
_PACKAGE_KEY_RE = re.compile(r"^(@[^/]+/[^@]+|[^@]+)@(.+)$")


def javascript_lockfile_kind(lockfile: str) -> str:
    """Return ``pnpm``, ``yarn``, or ``npm`` for a lockfile basename."""
    name = Path(lockfile).name
    if name in (PNPM_LOCKFILE, "pnpm-lock.yml"):
        return "pnpm"
    if name == YARN_LOCKFILE:
        return "yarn"
    return "npm"


def infer_javascript_lockfile(frontend_dir: Path) -> str | None:
    """Pick the JS lockfile present under a frontend directory."""
    if (frontend_dir / PNPM_LOCKFILE).is_file():
        return PNPM_LOCKFILE
    if (frontend_dir / YARN_LOCKFILE).is_file():
        return YARN_LOCKFILE
    if (frontend_dir / NPM_LOCKFILE).is_file():
        return NPM_LOCKFILE
    return None


def _uv_child_env() -> dict[str, str]:
    import os

    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    return env


def _run_json_cmd(cmd: list[str], *, cwd: Path) -> tuple[str, str, int]:
    result = run_cmd(
        cmd,
        cwd=cwd,
        env=_uv_child_env(),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout or "", result.stderr or "", result.returncode


def _normalize_license(value: str) -> str:
    text = value.strip()
    if not text or text.upper() in {"UNLICENSED", "UNKNOWN"}:
        return "UNKNOWN"
    return normalize_license_spdx(text)


def _license_from_raw(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        return text or None
    if isinstance(raw, dict):
        for key in ("type", "name", "id"):
            value = raw.get(key)
            if value:
                return str(value).strip()
        return None
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            part = _license_from_raw(item)
            if part:
                parts.append(part)
        return ", ".join(parts) if parts else None
    text = str(raw).strip()
    return text or None


def _license_from_package_json(data: dict) -> str:
    for field in ("license", "licenses"):
        parsed = _license_from_raw(data.get(field))
        if parsed:
            return _normalize_license(parsed)
    return "UNKNOWN"


def _parse_package_key(key: str) -> tuple[str, str] | None:
    match = _PACKAGE_KEY_RE.match(key)
    if not match:
        return None
    return match.group(1), match.group(2)


def _name_version_from_cache_zip(path: Path) -> tuple[str, str] | None:
    stem = path.name[:-4] if path.name.endswith(".zip") else path.stem
    if "-npm-" not in stem:
        return None
    body, sep, hash_suffix = stem.rpartition("-")
    if not sep or not _CACHE_HASH_RE.match(f"-{hash_suffix}"):
        return None
    prefix, version = body.rsplit("-npm-", 1)
    if prefix.startswith("@"):
        dash = prefix.find("-", 1)
        if dash < 0:
            return None
        full_name = f"@{prefix[1:dash]}/{prefix[dash + 1:]}"
    else:
        full_name = prefix
    return full_name, version


def _scan_yarn_pnp_cache(
    cwd: Path,
    *,
    source: str,
) -> list[CompliancePackage]:
    cache_dir = cwd / ".yarn" / "cache"
    if not cache_dir.is_dir():
        return []
    packages: list[CompliancePackage] = []
    seen: set[str] = set()
    for zip_path in sorted(cache_dir.glob("*.zip")):
        parsed = _name_version_from_cache_zip(zip_path)
        if parsed is None:
            continue
        name, version = parsed
        key = f"{name}@{version}"
        if key in seen:
            continue
        license_spdx = "UNKNOWN"
        try:
            with zipfile.ZipFile(zip_path) as archive:
                pkg_json_names = [
                    n
                    for n in archive.namelist()
                    if n.endswith("/package.json") and n.count("/") <= 2
                ]
                for entry in pkg_json_names:
                    data = json.loads(archive.read(entry))
                    if isinstance(data, dict):
                        license_spdx = _license_from_package_json(data)
                        if license_spdx != "UNKNOWN":
                            break
        except (OSError, json.JSONDecodeError, KeyError, zipfile.BadZipFile):
            license_spdx = "UNKNOWN"
        seen.add(key)
        packages.append(
            CompliancePackage(
                name=name,
                version=version,
                license_spdx=license_spdx,
                source=source,
            )
        )
    return packages


def _matching_snapshot_keys(snapshots: dict[str, Any], name: str, version: str) -> list[str]:
    prefix = f"{name}@{version}"
    return [key for key in snapshots if key == prefix or key.startswith(f"{prefix}(")]


def _dependency_version(value: Any) -> str:
    text = str(value)
    return text.split("(", 1)[0]


def _pnpm_reachable_packages(
    data: dict[str, Any],
    *,
    production_only: bool,
) -> set[tuple[str, str]]:
    importers = data.get("importers")
    if not isinstance(importers, dict):
        return set()
    importer = importers.get(".")
    if not isinstance(importer, dict) and importers:
        importer = next(iter(importers.values()))
    if not isinstance(importer, dict):
        return set()

    snapshots = data.get("snapshots")
    if not isinstance(snapshots, dict):
        snapshots = {}

    sections = ("dependencies",) if production_only else ("dependencies", "devDependencies")
    seeds: list[tuple[str, str]] = []
    for section in sections:
        block = importer.get(section)
        if not isinstance(block, dict):
            continue
        for name, meta in block.items():
            if isinstance(meta, dict) and meta.get("version"):
                seeds.append((str(name), _dependency_version(meta["version"])))

    reachable: set[tuple[str, str]] = set()
    queue = list(seeds)
    while queue:
        name, version = queue.pop()
        key = (name, version)
        if key in reachable:
            continue
        reachable.add(key)
        for snap_key in _matching_snapshot_keys(snapshots, name, version):
            snap = snapshots.get(snap_key)
            if not isinstance(snap, dict):
                continue
            for dep_section in ("dependencies", "optionalDependencies"):
                deps = snap.get(dep_section)
                if not isinstance(deps, dict):
                    continue
                for dep_name, dep_ver in deps.items():
                    queue.append((str(dep_name), _dependency_version(dep_ver)))
    return reachable


def _pnpm_packages_from_lockfile(
    data: dict[str, Any],
    *,
    production_only: bool,
) -> list[tuple[str, str]]:
    if production_only:
        return sorted(_pnpm_reachable_packages(data, production_only=True))

    packages_section = data.get("packages")
    if not isinstance(packages_section, dict):
        return []
    result: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key in packages_section:
        parsed = _parse_package_key(str(key))
        if parsed is None or parsed in seen:
            continue
        seen.add(parsed)
        result.append(parsed)
    return sorted(result)


def _license_from_adjacent_files(package_dir: Path) -> str:
    for filename in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.md"):
        path = package_dir / filename
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        upper = text.upper()
        if "APACHE LICENSE" in upper and "2.0" in upper:
            return "Apache-2.0"
        if "BSD-3-CLAUSE" in upper or "BSD 3-CLAUSE" in upper:
            return "BSD-3-Clause"
        if "BSD-2-CLAUSE" in upper or "BSD 2-CLAUSE" in upper:
            return "BSD-2-Clause"
        if "THE MIT LICENSE" in upper or "MIT LICENSE" in upper:
            return "MIT"
        if "ISC LICENSE" in upper:
            return "ISC"
    return "UNKNOWN"


def _build_node_modules_license_index(cwd: Path) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    node_modules = cwd / "node_modules"
    if not node_modules.is_dir():
        return index

    candidates: list[Path] = []
    candidates.extend(node_modules.glob("*/package.json"))
    candidates.extend(node_modules.glob("@*/*/package.json"))
    pnpm_dir = node_modules / ".pnpm"
    if pnpm_dir.is_dir():
        candidates.extend(pnpm_dir.glob("*/node_modules/*/package.json"))
        candidates.extend(pnpm_dir.glob("*/node_modules/@*/*/package.json"))

    for pkg_json in candidates:
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        version = data.get("version")
        if not name or not version:
            continue
        license_spdx = _license_from_package_json(data)
        if license_spdx == "UNKNOWN":
            license_spdx = _license_from_adjacent_files(pkg_json.parent)
        if license_spdx == "UNKNOWN":
            continue
        index[(str(name), str(version))] = license_spdx
    return index


def parse_pnpm_lockfile_packages(
    data: dict[str, Any],
    *,
    source: str,
    production_only: bool,
    license_index: dict[tuple[str, str], str] | None = None,
) -> list[CompliancePackage]:
    """Parse pnpm lockfile data into compliance packages (for tests and scan)."""
    license_index = license_index or {}
    packages: list[CompliancePackage] = []
    for name, version in _pnpm_packages_from_lockfile(data, production_only=production_only):
        license_spdx = license_index.get((name, version), "UNKNOWN")
        packages.append(
            CompliancePackage(
                name=name,
                version=version,
                license_spdx=license_spdx,
                source=source,
            )
        )
    return packages


def _scan_pnpm_lockfile(
    cwd: Path,
    lock_path: Path,
    *,
    source: str,
    production_only: bool,
) -> list[CompliancePackage]:
    try:
        data = yaml.safe_load(lock_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(data, dict):
        return []
    license_index = _build_node_modules_license_index(cwd)
    return parse_pnpm_lockfile_packages(
        data,
        source=source,
        production_only=production_only,
        license_index=license_index,
    )


def _extract_json_object(text: str) -> dict | None:
    start = text.find("{")
    if start < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _package_name_from_key(key: str) -> tuple[str, str]:
    parsed = _parse_package_key(key)
    if parsed is None:
        return key, ""
    return parsed


def parse_license_checker_json(
    payload: dict,
    *,
    source: str,
    production_only: bool,
) -> list[CompliancePackage]:
    packages: list[CompliancePackage] = []
    for key, meta in sorted(payload.items()):
        if not isinstance(meta, dict):
            continue
        if meta.get("private") and str(meta.get("licenses") or "").upper() == "UNLICENSED":
            continue
        if production_only and meta.get("devDependency"):
            continue
        name, version = _package_name_from_key(str(key))
        license_spdx = "UNKNOWN"
        for field in ("licenses", "license"):
            parsed = _license_from_raw(meta.get(field))
            if parsed:
                license_spdx = _normalize_license(parsed)
                break
        packages.append(
            CompliancePackage(
                name=name,
                version=version,
                license_spdx=license_spdx,
                source=source,
            )
        )
    return packages


def _license_checker_invoker(package_manager: str) -> list[str]:
    if package_manager == "pnpm":
        return ["pnpm", "dlx"]
    if package_manager == "yarn":
        return ["yarn", "dlx"]
    return ["npx", "--yes"]


def _scan_license_checker(
    cwd: Path,
    *,
    source: str,
    production_only: bool,
    package_manager: str,
) -> tuple[list[CompliancePackage], str | None]:
    cmd = [*_license_checker_invoker(package_manager), "license-checker", "--json"]
    if production_only:
        cmd.append("--production")
    stdout, stderr, code = _run_json_cmd(cmd, cwd=cwd)
    if code != 0:
        return [], (stderr or stdout or "license-checker failed").strip()
    payload = _extract_json_object(stdout + "\n" + stderr)
    if payload is None:
        return [], "license-checker returned invalid JSON"
    return parse_license_checker_json(payload, source=source, production_only=production_only), None


def _needs_license_checker_fallback(packages: list[CompliancePackage]) -> bool:
    if not packages:
        return True
    unknown = sum(1 for pkg in packages if pkg.license_spdx == "UNKNOWN")
    return unknown > len(packages) // 2


def _javascript_install_hint(kind: str) -> str:
    if kind == "pnpm":
        return "pnpm install"
    if kind == "yarn":
        return "yarn install"
    return "npm install"


def _javascript_install_missing(cwd: Path, *, kind: str) -> bool:
    """True when the package manager install tree needed for license lookup is absent."""
    if kind == "pnpm":
        return not (cwd / "node_modules").is_dir()
    if kind == "yarn":
        return not (cwd / ".yarn" / "cache").is_dir() and not (cwd / "node_modules").is_dir()
    return not (cwd / "node_modules").is_dir()


def scan_javascript(
    config: ProjectConfig,
    js_config: ComplianceJavascriptConfig,
) -> tuple[list[CompliancePackage], list[ComplianceViolation]]:
    cwd_rel = js_config.cwd or config.paths.frontend
    cwd = config.repo_root / cwd_rel
    lock_path = cwd / js_config.lockfile
    source = f"javascript:{cwd_rel}"
    violations: list[ComplianceViolation] = []
    if not lock_path.is_file():
        violations.append(
            ComplianceViolation(
                code="missing_javascript_lockfile",
                message=f"JavaScript lockfile not found: {cwd_rel}/{js_config.lockfile}",
            )
        )
        return [], violations

    kind = javascript_lockfile_kind(js_config.lockfile)
    if _javascript_install_missing(cwd, kind=kind):
        hint = _javascript_install_hint(kind)
        detail = (
            "missing node_modules"
            if kind != "yarn"
            else "missing .yarn/cache and node_modules"
        )
        violations.append(
            ComplianceViolation(
                code="javascript_install_required",
                message=(
                    f"JavaScript dependencies not installed in {cwd_rel} ({detail}). "
                    f"Run: {hint}"
                ),
            )
        )
        return [], violations

    packages: list[CompliancePackage] = []
    err: str | None = None

    if kind == "pnpm":
        packages = _scan_pnpm_lockfile(
            cwd,
            lock_path,
            source=source,
            production_only=js_config.production_only,
        )
        if _needs_license_checker_fallback(packages):
            packages_lc, err = _scan_license_checker(
                cwd,
                source=source,
                production_only=js_config.production_only,
                package_manager="pnpm",
            )
            if packages_lc:
                packages = packages_lc
    elif kind == "yarn":
        packages = _scan_yarn_pnp_cache(cwd, source=source)
        if not packages:
            packages, err = _scan_license_checker(
                cwd,
                source=source,
                production_only=js_config.production_only,
                package_manager="yarn",
            )
    else:
        packages, err = _scan_license_checker(
            cwd,
            source=source,
            production_only=js_config.production_only,
            package_manager="npm",
        )

    if not packages:
        if err:
            violations.append(
                ComplianceViolation(
                    code="javascript_scan_failed",
                    message=f"JavaScript license scan failed in {cwd_rel}: {err}",
                )
            )
        return [], violations
    return packages, violations


def parse_yarn_licenses_json(
    payload: dict,
    *,
    source: str,
) -> list[CompliancePackage]:
    """Parse yarn-shaped fixture payloads for unit tests."""
    packages: list[CompliancePackage] = []
    for key in ("dependencies",):
        block = payload.get(key)
        if not isinstance(block, dict):
            continue
        for name, meta in sorted(block.items()):
            if not isinstance(meta, dict):
                continue
            licenses = meta.get("licenses")
            if isinstance(licenses, list):
                license_spdx = ", ".join(str(x) for x in licenses if x) or "UNKNOWN"
            else:
                license_spdx = str(licenses or "UNKNOWN")
            packages.append(
                CompliancePackage(
                    name=str(name),
                    version=str(meta.get("version") or ""),
                    license_spdx=_normalize_license(license_spdx),
                    source=source,
                )
            )
    return packages
