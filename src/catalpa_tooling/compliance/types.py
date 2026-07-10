"""Shared types for OSS compliance scanning."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CompliancePackage:
    """One dependency or bundled component with a license identifier."""

    name: str
    version: str
    license_spdx: str
    source: str  # e.g. python:platform/docker/uv.lock, javascript:frontend


@dataclass
class ComplianceViolation:
    """Human-readable policy or configuration failure."""

    code: str
    message: str
    severity: str = "error"  # error | warn


@dataclass
class ComplianceScanResult:
    """Aggregated output from all scanners."""

    packages: list[CompliancePackage] = field(default_factory=list)
    violations: list[ComplianceViolation] = field(default_factory=list)
