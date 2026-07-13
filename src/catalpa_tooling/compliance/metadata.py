"""Project license metadata and required license file checks."""

from __future__ import annotations

import json
import re
from pathlib import Path

from catalpa_tooling.compliance.types import ComplianceViolation
from catalpa_tooling.config import ComplianceConfig, ProjectConfig


def _read_json_license(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("license")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, dict):
        for key in ("id", "name", "type"):
            if raw.get(key):
                return str(raw[key]).strip()
    return str(raw).strip() or None


def _read_pyproject_license(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(
        r'^\s*license\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|\{\s*text\s*=\s*"([^"]+)"\s*\})',
        text,
        flags=re.MULTILINE,
    )
    if not match:
        return None
    return next(g for g in match.groups() if g).strip()


def check_metadata(config: ProjectConfig, compliance: ComplianceConfig) -> list[ComplianceViolation]:
    violations: list[ComplianceViolation] = []
    expected = compliance.project_license.strip()

    for rel in compliance.license_files:
        path = config.repo_root / rel
        if not path.is_file():
            violations.append(
                ComplianceViolation(
                    code="missing_license_file",
                    message=f"Required license file not found: {rel}",
                )
            )

    frontend = config.frontend_dir
    for candidate, reader in (
        (frontend / "package.json", _read_json_license),
        (frontend / "pyproject.toml", _read_pyproject_license),
        (config.repo_root / "pyproject.toml", _read_pyproject_license),
    ):
        if not candidate.is_file():
            continue
        declared = reader(candidate)
        if declared and declared != expected:
            violations.append(
                ComplianceViolation(
                    code="license_metadata_mismatch",
                    message=(
                        f"{candidate.relative_to(config.repo_root)} declares {declared!r}; "
                        f"expected {expected!r} from tooling.yaml compliance.project_license"
                    ),
                    severity="warn",
                )
            )
    return violations
