"""Python production dependency license scan via uv export + pip-licenses."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from catalpa_tooling.compliance.licenses import normalize_license_spdx
from catalpa_tooling.compliance.types import CompliancePackage, ComplianceViolation
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.run_cmd import run as run_cmd


def _uv_child_env() -> dict[str, str]:
    import os

    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    return env


def _project_dir_for_lockfile(repo_root: Path, lockfile_rel: str) -> Path:
    lock_path = repo_root / lockfile_rel
    if lock_path.name == "uv.lock" and lock_path.parent.name == "docker":
        candidate = lock_path.parent.parent
        if (candidate / "pyproject.toml").is_file():
            return candidate
    parent = lock_path.parent
    if (parent / "pyproject.toml").is_file():
        return parent
    return repo_root


def _export_requirements(repo_root: Path, lockfile_rel: str) -> tuple[Path | None, str | None]:
    project_dir = _project_dir_for_lockfile(repo_root, lockfile_rel)
    rel_project = project_dir.relative_to(repo_root)
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    cmd = [
        "uv",
        "export",
        "--project",
        str(rel_project),
        "--locked",
        "--no-dev",
        "--no-emit-project",
        "--no-header",
        "-o",
        str(tmp_path),
    ]
    result = run_cmd(cmd, cwd=repo_root, env=_uv_child_env(), check=False, capture_output=True, text=True)
    if result.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout or "").strip()
        return None, detail or "uv export failed"
    return tmp_path, None


def _pip_licenses_from_requirements(
    repo_root: Path,
    requirements: Path,
) -> tuple[list[CompliancePackage], str | None]:
    """Install exported requirements into an ephemeral venv, then run pip-licenses."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="compliance-py-") as tmp_dir:
        venv_dir = Path(tmp_dir) / "venv"
        venv_python = venv_dir / "bin" / "python"
        for cmd in (
            ["uv", "venv", str(venv_dir)],
            ["uv", "pip", "install", "-r", str(requirements), "--python", str(venv_python)],
        ):
            result = run_cmd(cmd, cwd=repo_root, env=_uv_child_env(), check=False, capture_output=True, text=True)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                return [], detail or " ".join(cmd[:2]) + " failed"
        cmd = [
            "uv",
            "run",
            "--group",
            "compliance",
            "pip-licenses",
            "--format=json",
            "--with-urls",
            "--python",
            str(venv_python),
        ]
        result = run_cmd(cmd, cwd=repo_root, env=_uv_child_env(), check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return [], detail or "pip-licenses failed"
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return [], "pip-licenses returned invalid JSON"
    if not isinstance(rows, list):
        return [], "pip-licenses returned unexpected payload"
    packages: list[CompliancePackage] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or row.get("name") or "").strip()
        version = str(row.get("Version") or row.get("version") or "").strip()
        license_name = normalize_license_spdx(
            str(row.get("License") or row.get("license") or "UNKNOWN").strip() or "UNKNOWN"
        )
        if name:
            packages.append(
                CompliancePackage(
                    name=name,
                    version=version,
                    license_spdx=license_name,
                    source="python",
                )
            )
    return packages, None


def scan_python_lockfiles(
    config: ProjectConfig,
    lockfiles: tuple[str, ...],
) -> tuple[list[CompliancePackage], list[ComplianceViolation]]:
    packages: list[CompliancePackage] = []
    violations: list[ComplianceViolation] = []
    for lockfile_rel in lockfiles:
        lock_path = config.repo_root / lockfile_rel
        if not lock_path.is_file():
            violations.append(
                ComplianceViolation(
                    code="missing_python_lockfile",
                    message=f"Python lockfile not found: {lockfile_rel}",
                )
            )
            continue
        requirements, err = _export_requirements(config.repo_root, lockfile_rel)
        if requirements is None:
            violations.append(
                ComplianceViolation(
                    code="python_export_failed",
                    message=f"Could not export requirements from {lockfile_rel}: {err}",
                )
            )
            continue
        try:
            found, scan_err = _pip_licenses_from_requirements(config.repo_root, requirements)
        finally:
            requirements.unlink(missing_ok=True)
        if scan_err:
            violations.append(
                ComplianceViolation(
                    code="python_scan_failed",
                    message=f"pip-licenses failed for {lockfile_rel}: {scan_err}",
                )
            )
            continue
        for pkg in found:
            packages.append(
                CompliancePackage(
                    name=pkg.name,
                    version=pkg.version,
                    license_spdx=pkg.license_spdx,
                    source=f"python:{lockfile_rel}",
                )
            )
    return packages, violations


def scan_python_lockfiles_offline(
    lockfile_text: str,
    *,
    source: str,
) -> list[CompliancePackage]:
    """Parse a minimal uv.lock TOML snippet for unit tests without uv/pip-licenses."""
    packages: list[CompliancePackage] = []
    current_name: str | None = None
    current_version: str | None = None
    for line in lockfile_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("name = "):
            current_name = stripped.split("=", 1)[1].strip().strip('"')
        elif stripped.startswith("version = "):
            current_version = stripped.split("=", 1)[1].strip().strip('"')
        elif stripped == "[[package]]":
            if current_name:
                packages.append(
                    CompliancePackage(
                        name=current_name,
                        version=current_version or "",
                        license_spdx="UNKNOWN",
                        source=source,
                    )
                )
            current_name = None
            current_version = None
    if current_name:
        packages.append(
            CompliancePackage(
                name=current_name,
                version=current_version or "",
                license_spdx="UNKNOWN",
                source=source,
            )
        )
    return packages
