"""Unit tests for ``tests compliance`` orchestration."""

from __future__ import annotations

import json
from unittest.mock import patch

import yaml

from catalpa_tooling.compliance.javascript_scan import (
    parse_pnpm_lockfile_packages,
    parse_yarn_licenses_json,
    scan_javascript,
)
from catalpa_tooling.compliance.notices import render_notices
from catalpa_tooling.compliance.policy import check_license_policy
from catalpa_tooling.compliance.types import CompliancePackage
from catalpa_tooling.compliance_cli import run_compliance
from catalpa_tooling.config import (
    ComplianceBundledAssetConfig,
    ComplianceConfig,
    ComplianceJavascriptConfig,
    ComplianceOutputsConfig,
    CompliancePythonConfig,
)

_PNPM_FIXTURE = {
    "lockfileVersion": "9.0",
    "importers": {
        ".": {
            "dependencies": {
                "chalk": {"version": "2.4.2"},
            },
            "devDependencies": {
                "typescript": {"version": "5.0.0"},
            },
        }
    },
    "packages": {
        "chalk@2.4.2": {},
        "ansi-styles@3.2.1": {},
        "typescript@5.0.0": {},
    },
    "snapshots": {
        "chalk@2.4.2": {
            "dependencies": {
                "ansi-styles": "3.2.1",
            }
        },
        "ansi-styles@3.2.1": {},
        "typescript@5.0.0": {},
    },
}


def _compliance_config() -> ComplianceConfig:
    return ComplianceConfig(
        project_license="AGPL-3.0-or-later",
        license_files=("frontend/LICENSE",),
        python=CompliancePythonConfig(lockfiles=("frontend/docker/uv.lock",)),
        javascript=ComplianceJavascriptConfig(
            cwd="frontend",
            lockfile="yarn.lock",
            production_only=True,
        ),
        bundled_assets=(
            ComplianceBundledAssetConfig(
                path="frontend/src/fonts",
                license_globs=("*OFL*",),
            ),
        ),
        forbidden_spdx=("UNLICENSED",),
        warn_spdx=("UNKNOWN", "GPL-3.0-only"),
        allow_strong_copyleft=True,
        outputs=ComplianceOutputsConfig(
            sbom_dir="compliance/sbom",
            notices="compliance/THIRD_PARTY_NOTICES.md",
        ),
    )


def _seed_repo(config) -> None:
    root = config.repo_root
    (root / "frontend" / "LICENSE").write_text("AGPL\n", encoding="utf-8")
    (root / "frontend" / "docker").mkdir(parents=True, exist_ok=True)
    (root / "frontend" / "docker" / "uv.lock").write_text(
        '[[package]]\nname = "django"\nversion = "5.0"\n',
        encoding="utf-8",
    )
    (root / "frontend" / "yarn.lock").write_text("# yarn\n", encoding="utf-8")
    (root / "frontend" / "src" / "fonts").mkdir(parents=True, exist_ok=True)
    (root / "frontend" / "src" / "fonts" / "OFL.txt").write_text("OFL\n", encoding="utf-8")


def test_policy_forbidden_license() -> None:
    packages = [
        CompliancePackage(
            name="secret-lib",
            version="1.0",
            license_spdx="UNLICENSED",
            source="python:test",
        )
    ]
    violations = check_license_policy(
        packages,
        forbidden_spdx=("UNLICENSED",),
        warn_spdx=("UNKNOWN",),
        allow_strong_copyleft=False,
    )
    assert len(violations) == 1
    assert violations[0].code == "forbidden_license"


def test_parse_yarn_licenses_json_fixture() -> None:
    payload = {
        "dependencies": {
            "lodash": {"version": "4.0.0", "licenses": ["MIT"]},
        }
    }
    packages = parse_yarn_licenses_json(payload, source="javascript:frontend")
    assert len(packages) == 1
    assert packages[0].name == "lodash"
    assert packages[0].license_spdx == "MIT"


def test_parse_pnpm_lockfile_production_only() -> None:
    packages = parse_pnpm_lockfile_packages(
        _PNPM_FIXTURE,
        source="javascript:frontend",
        production_only=True,
        license_index={("chalk", "2.4.2"): "MIT", ("ansi-styles", "3.2.1"): "MIT"},
    )
    names = {pkg.name for pkg in packages}
    assert names == {"chalk", "ansi-styles"}
    assert "typescript" not in names


def test_parse_pnpm_lockfile_strips_peer_suffix_from_importer_version() -> None:
    fixture = {
        "importers": {
            ".": {
                "dependencies": {
                    "ua-parser-js": {"version": "2.0.4(encoding@0.1.13)"},
                }
            }
        },
        "snapshots": {
            "ua-parser-js@2.0.4(encoding@0.1.13)": {},
        },
    }
    packages = parse_pnpm_lockfile_packages(
        fixture,
        source="javascript:frontend",
        production_only=True,
        license_index={("ua-parser-js", "2.0.4"): "AGPL-3.0-or-later"},
    )
    assert len(packages) == 1
    assert packages[0].version == "2.0.4"
    assert packages[0].license_spdx == "AGPL-3.0-or-later"


def test_parse_pnpm_lockfile_includes_dev_dependencies() -> None:
    packages = parse_pnpm_lockfile_packages(
        _PNPM_FIXTURE,
        source="javascript:frontend",
        production_only=False,
    )
    names = {pkg.name for pkg in packages}
    assert "typescript" in names
    assert "chalk" in names


def test_license_from_package_json_licenses_array_with_dict() -> None:
    from catalpa_tooling.compliance.javascript_scan import _license_from_package_json

    data = {
        "licenses": [
            {
                "type": "MIT",
                "url": "http://github.com/Raynos/min-document/raw/master/LICENSE",
            }
        ]
    }
    assert _license_from_package_json(data) == "MIT"


def test_parse_license_checker_json_dict_license() -> None:
    from catalpa_tooling.compliance.javascript_scan import parse_license_checker_json

    payload = {
        "min-document@2.19.0": {
            "licenses": [
                {
                    "type": "MIT",
                    "url": "http://github.com/Raynos/min-document/raw/master/LICENSE",
                }
            ]
        }
    }
    packages = parse_license_checker_json(
        payload, source="javascript:frontend", production_only=True
    )
    assert len(packages) == 1
    assert packages[0].license_spdx == "MIT"


def test_render_notices_stable_sort() -> None:
    packages = [
        CompliancePackage("zebra", "1", "MIT", "javascript:frontend"),
        CompliancePackage("alpha", "2", "BSD-3-Clause", "python:lock"),
    ]
    text = render_notices(packages, project_name="demo", project_license="AGPL-3.0-or-later")
    assert "## JavaScript" in text
    assert "## Python" in text
    assert "alpha" in text
    assert "zebra" in text
    assert text.index("## JavaScript") < text.index("## Python")
    assert text.index("zebra") < text.index("alpha")
    assert "| Source |" not in text


def test_render_notices_python_source_column_when_multiple_lockfiles() -> None:
    packages = [
        CompliancePackage("django", "5.0", "BSD-3-Clause", "python:platform/docker/uv.lock"),
        CompliancePackage("requests", "2.32", "Apache-2.0", "python:other/uv.lock"),
    ]
    text = render_notices(packages, project_name="demo", project_license="MIT")
    assert "## Python" in text
    assert "| Source |" in text
    assert "python:platform/docker/uv.lock" in text


def _pnpm_js_config() -> ComplianceJavascriptConfig:
    return ComplianceJavascriptConfig(
        cwd="frontend",
        lockfile="pnpm-lock.yaml",
        production_only=True,
    )


def _write_pnpm_lockfile(frontend: Path) -> None:
    frontend.mkdir(parents=True, exist_ok=True)
    (frontend / "pnpm-lock.yaml").write_text(
        yaml.safe_dump(_PNPM_FIXTURE),
        encoding="utf-8",
    )


def test_scan_javascript_requires_pnpm_install(minimal_project) -> None:
    config = minimal_project
    frontend = config.repo_root / "frontend"
    _write_pnpm_lockfile(frontend)

    packages, violations = scan_javascript(config, _pnpm_js_config())

    assert packages == []
    assert len(violations) == 1
    assert violations[0].code == "javascript_install_required"
    assert violations[0].severity == "error"
    assert "pnpm install" in violations[0].message
    assert "node_modules" in violations[0].message


def test_scan_javascript_resolves_licenses_from_node_modules(minimal_project) -> None:
    config = minimal_project
    frontend = config.repo_root / "frontend"
    _write_pnpm_lockfile(frontend)

    chalk_dir = frontend / "node_modules" / "chalk"
    chalk_dir.mkdir(parents=True, exist_ok=True)
    (chalk_dir / "package.json").write_text(
        json.dumps({"name": "chalk", "version": "2.4.2", "license": "MIT"}),
        encoding="utf-8",
    )
    ansi_dir = frontend / "node_modules" / "ansi-styles"
    ansi_dir.mkdir(parents=True, exist_ok=True)
    (ansi_dir / "package.json").write_text(
        json.dumps({"name": "ansi-styles", "version": "3.2.1", "license": "MIT"}),
        encoding="utf-8",
    )

    packages, violations = scan_javascript(config, _pnpm_js_config())

    assert violations == []
    by_name = {pkg.name: pkg for pkg in packages}
    assert by_name["chalk"].license_spdx == "MIT"
    assert by_name["ansi-styles"].license_spdx == "MIT"
    assert "typescript" not in by_name


def test_run_compliance_fails_on_javascript_install_required(minimal_project) -> None:
    config = minimal_project
    _seed_repo(config)
    (config.repo_root / "frontend" / "pnpm-lock.yaml").write_text(
        yaml.safe_dump(_PNPM_FIXTURE),
        encoding="utf-8",
    )
    compliance = _compliance_config()
    object.__setattr__(
        compliance,
        "javascript",
        ComplianceJavascriptConfig(
            cwd="frontend",
            lockfile="pnpm-lock.yaml",
            production_only=True,
        ),
    )
    object.__setattr__(config, "compliance", compliance)

    with patch(
        "catalpa_tooling.compliance_cli.scan_python_lockfiles",
        return_value=([], []),
    ):
        rc = run_compliance(config, check_only=False, ci_mode=True)

    assert rc == 1


def test_run_compliance_check_only_missing_artifacts(minimal_project) -> None:
    config = minimal_project
    _seed_repo(config)
    object.__setattr__(config, "compliance", _compliance_config())

    py_packages = [
        CompliancePackage("django", "5.0", "BSD-3-Clause", "python:frontend/docker/uv.lock")
    ]
    js_packages = [
        CompliancePackage("lodash", "4.0.0", "MIT", "javascript:frontend"),
    ]

    with (
        patch("catalpa_tooling.compliance_cli.scan_python_lockfiles", return_value=(py_packages, [])),
        patch("catalpa_tooling.compliance_cli.scan_javascript", return_value=(js_packages, [])),
        patch("catalpa_tooling.compliance_cli.write_python_sbom", return_value=[]),
    ):
        rc = run_compliance(config, check_only=True, ci_mode=True)

    assert rc == 1


def test_run_compliance_writes_artifacts(minimal_project) -> None:
    config = minimal_project
    _seed_repo(config)
    object.__setattr__(config, "compliance", _compliance_config())

    py_packages = [
        CompliancePackage("django", "5.0", "BSD-3-Clause", "python:frontend/docker/uv.lock")
    ]
    js_packages = [
        CompliancePackage("lodash", "4.0.0", "MIT", "javascript:frontend"),
    ]

    with (
        patch("catalpa_tooling.compliance_cli.scan_python_lockfiles", return_value=(py_packages, [])),
        patch("catalpa_tooling.compliance_cli.scan_javascript", return_value=(js_packages, [])),
        patch("catalpa_tooling.compliance_cli.write_python_sbom", return_value=[]),
    ):
        rc = run_compliance(config, check_only=False, ci_mode=True)

    assert rc == 0
    notices = config.repo_root / "compliance" / "THIRD_PARTY_NOTICES.md"
    assert notices.is_file()
    assert "django" in notices.read_text(encoding="utf-8")
    merged = config.repo_root / "compliance" / "sbom" / "bom.cdx.json"
    assert merged.is_file()
    bom = json.loads(merged.read_text(encoding="utf-8"))
    assert bom["components"]
