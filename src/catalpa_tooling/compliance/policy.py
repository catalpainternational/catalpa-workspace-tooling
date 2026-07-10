"""License policy checks (forbidden / warn SPDX tiers)."""

from __future__ import annotations

from catalpa_tooling.compliance.licenses import license_tokens, normalize_license_spdx
from catalpa_tooling.compliance.types import CompliancePackage, ComplianceViolation


def _normalize_spdx(value: str) -> str:
    return value.strip().upper().replace(" ", "-")


def _matches_tier(license_spdx: str, tier: tuple[str, ...]) -> bool:
    tier_keys = {_normalize_spdx(x) for x in tier}
    for token in license_tokens(license_spdx):
        normalized = _normalize_spdx(token)
        if not normalized or normalized in {"UNKNOWN", "N/A"}:
            if "UNKNOWN" in tier_keys:
                return True
            continue
        if normalized in tier_keys:
            return True
    return False


def check_license_policy(
    packages: list[CompliancePackage],
    *,
    forbidden_spdx: tuple[str, ...],
    warn_spdx: tuple[str, ...],
    allow_strong_copyleft: bool,
) -> list[ComplianceViolation]:
    violations: list[ComplianceViolation] = []
    effective_warn = warn_spdx
    if allow_strong_copyleft:
        strong = {"GPL-2.0-ONLY", "GPL-3.0-ONLY", "GPL-2.0-OR-LATER", "GPL-3.0-OR-LATER"}
        effective_warn = tuple(x for x in warn_spdx if _normalize_spdx(x) not in strong)

    for pkg in packages:
        spdx = normalize_license_spdx(pkg.license_spdx.strip())
        if _matches_tier(spdx, forbidden_spdx):
            violations.append(
                ComplianceViolation(
                    code="forbidden_license",
                    message=f"{pkg.name} ({pkg.source}) has forbidden license {spdx!r}",
                )
            )
            continue
        if _matches_tier(spdx, effective_warn):
            violations.append(
                ComplianceViolation(
                    code="warn_license",
                    message=f"{pkg.name} ({pkg.source}) has warn-tier license {spdx!r}",
                    severity="warn",
                )
            )
    return violations
