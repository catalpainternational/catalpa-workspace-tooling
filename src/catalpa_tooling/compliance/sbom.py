"""CycloneDX SBOM generation and drift detection."""

from __future__ import annotations

import json
from pathlib import Path

from catalpa_tooling.compliance.types import ComplianceViolation
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.run_cmd import run as run_cmd


def _uv_child_env() -> dict[str, str]:
    import os

    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    return env


def _export_requirements(repo_root: Path, lockfile_rel: str) -> Path | None:
    from catalpa_tooling.compliance.python_scan import _export_requirements as export

    path, _ = export(repo_root, lockfile_rel)
    return path


def _normalize_cyclonedx_bom(data: dict) -> dict:
    """Drop volatile cyclonedx-py fields so SBOM drift checks are stable."""
    data = dict(data)
    data.pop("serialNumber", None)
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        metadata = dict(metadata)
        metadata.pop("timestamp", None)
        if metadata:
            data["metadata"] = metadata
        else:
            data.pop("metadata", None)
    return data


def _normalize_cyclonedx_text(text: str) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(data, dict):
        return text
    return json.dumps(_normalize_cyclonedx_bom(data), indent=2, sort_keys=True) + "\n"


def write_python_sbom(
    config: ProjectConfig,
    lockfile_rel: str,
    output_path: Path,
) -> list[ComplianceViolation]:
    violations: list[ComplianceViolation] = []
    requirements = _export_requirements(config.repo_root, lockfile_rel)
    if requirements is None:
        violations.append(
            ComplianceViolation(
                code="sbom_python_export_failed",
                message=f"Could not export requirements for SBOM from {lockfile_rel}",
            )
        )
        return violations
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "uv",
        "run",
        "--group",
        "compliance",
        "cyclonedx-py",
        "requirements",
        str(requirements),
        "--of",
        "JSON",
        "-o",
        str(output_path),
    ]
    try:
        result = run_cmd(cmd, cwd=config.repo_root, env=_uv_child_env(), check=False, capture_output=True, text=True)
    finally:
        requirements.unlink(missing_ok=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        violations.append(
            ComplianceViolation(
                code="sbom_python_failed",
                message=f"cyclonedx-py failed for {lockfile_rel}: {detail or 'non-zero exit'}",
            )
        )
    elif output_path.is_file():
        output_path.write_text(
            _normalize_cyclonedx_text(output_path.read_text(encoding="utf-8")),
            encoding="utf-8",
        )
    return violations


def write_javascript_sbom_stub(
    packages: list,
    output_path: Path,
) -> None:
    """Write a minimal CycloneDX document from JavaScript license scan results."""
    components = []
    for pkg in packages:
        if not pkg.source.startswith("javascript:"):
            continue
        components.append(
            {
                "type": "library",
                "name": pkg.name,
                "version": pkg.version or None,
                "licenses": [{"license": {"name": pkg.license_spdx}}],
            }
        )
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "components": components,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(bom, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_merged_sbom(sbom_paths: list[Path], output_path: Path) -> None:
    components: list[dict] = []
    for path in sbom_paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        block = data.get("components")
        if isinstance(block, list):
            components.extend(block)
    components.sort(key=lambda c: (c.get("name") or "", c.get("version") or ""))
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "components": components,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(bom, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_file_drift(
    expected_text: str,
    committed_path: Path,
    *,
    label: str,
) -> list[ComplianceViolation]:
    if not committed_path.is_file():
        return [
            ComplianceViolation(
                code="missing_committed_artifact",
                message=f"Committed compliance artifact missing: {label} ({committed_path})",
            )
        ]
    committed = committed_path.read_text(encoding="utf-8")
    if label.endswith(".cdx.json"):
        expected_text = _normalize_cyclonedx_text(expected_text)
        committed = _normalize_cyclonedx_text(committed)
    if committed != expected_text:
        return [
            ComplianceViolation(
                code="stale_compliance_artifact",
                message=f"Committed {label} is out of date; run `uv run test compliance` to regenerate",
            )
        ]
    return []
