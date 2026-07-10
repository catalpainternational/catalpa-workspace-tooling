"""Bundled asset directories must include license files matching configured globs."""

from __future__ import annotations

from pathlib import Path

from catalpa_tooling.compliance.types import ComplianceViolation
from catalpa_tooling.config import ComplianceBundledAssetConfig, ProjectConfig


def check_bundled_assets(
    config: ProjectConfig,
    assets: tuple[ComplianceBundledAssetConfig, ...],
) -> list[ComplianceViolation]:
    violations: list[ComplianceViolation] = []
    for asset in assets:
        root = config.repo_root / asset.path
        if not root.is_dir():
            violations.append(
                ComplianceViolation(
                    code="missing_bundled_asset_dir",
                    message=f"Bundled asset directory not found: {asset.path}",
                )
            )
            continue
        matched = False
        for pattern in asset.license_globs:
            if any(root.glob(pattern)):
                matched = True
                break
        if not matched:
            globs = ", ".join(asset.license_globs)
            violations.append(
                ComplianceViolation(
                    code="missing_bundled_license",
                    message=f"No license file matching [{globs}] under {asset.path}",
                )
            )
    return violations
