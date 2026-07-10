"""Tests for license string normalization."""

from __future__ import annotations

from catalpa_tooling.compliance.licenses import normalize_license_spdx, normalize_license_token
from catalpa_tooling.compliance.policy import check_license_policy
from catalpa_tooling.compliance.types import CompliancePackage


def test_normalize_pillow_heif_gplv2() -> None:
    assert (
        normalize_license_token("GNU General Public License v2 (GPLv2)")
        == "GPL-2.0-only"
    )


def test_normalize_crontab_lgpl_compound() -> None:
    raw = (
        "GNU Lesser General Public License v2 (LGPLv2); "
        "GNU Lesser General Public License v3 (LGPLv3); "
        "GNU Library or Lesser General Public License (LGPL)"
    )
    assert normalize_license_spdx(raw) == "LGPL-2.0-or-later; LGPL-3.0-or-later"


def test_normalize_qrcode_proprietary_compound() -> None:
    assert normalize_license_spdx("BSD License; Other/Proprietary License") == "BSD-3-Clause"


def test_normalize_qrcode_proprietary_only() -> None:
    assert normalize_license_spdx("Other/Proprietary License") == "LicenseRef-proprietary"


def test_normalize_preserves_spdx_from_js() -> None:
    assert normalize_license_spdx("LGPL-2.0-or-later") == "LGPL-2.0-or-later"
    assert normalize_license_spdx("MIT") == "MIT"


def test_policy_warns_on_normalized_gpl_when_copyleft_not_allowed() -> None:
    packages = [
        CompliancePackage(
            "pillow_heif",
            "1.4.0",
            "GNU General Public License v2 (GPLv2)",
            "python:platform/docker/uv.lock",
        )
    ]
    violations = check_license_policy(
        packages,
        forbidden_spdx=(),
        warn_spdx=("GPL-2.0-only",),
        allow_strong_copyleft=False,
    )
    assert len(violations) == 1
    assert violations[0].code == "warn_license"


def test_policy_skips_gpl_warn_when_allow_strong_copyleft() -> None:
    packages = [
        CompliancePackage(
            "pillow_heif",
            "1.4.0",
            "GNU General Public License v2 (GPLv2)",
            "python:platform/docker/uv.lock",
        )
    ]
    violations = check_license_policy(
        packages,
        forbidden_spdx=(),
        warn_spdx=("GPL-2.0-only",),
        allow_strong_copyleft=True,
    )
    assert violations == []
