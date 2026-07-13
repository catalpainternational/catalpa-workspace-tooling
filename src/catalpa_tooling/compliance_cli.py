"""``tests compliance`` — OSS license scan, SBOM, and policy gate for consumer repos."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from catalpa_tooling.compliance.bundled_assets import check_bundled_assets
from catalpa_tooling.compliance.javascript_scan import scan_javascript
from catalpa_tooling.compliance.metadata import check_metadata
from catalpa_tooling.compliance.notices import render_notices
from catalpa_tooling.compliance.policy import check_license_policy
from catalpa_tooling.compliance.python_scan import scan_python_lockfiles
from catalpa_tooling.compliance.sbom import (
    check_file_drift,
    write_javascript_sbom_stub,
    write_merged_sbom,
    write_python_sbom,
)
from catalpa_tooling.compliance.types import ComplianceViolation
from catalpa_tooling.config import ProjectConfig, resolve_compliance_config


def _print_violations(violations: list[ComplianceViolation]) -> None:
    for item in violations:
        prefix = "compliance: ERROR" if item.severity == "error" else "compliance: WARN"
        print(f"{prefix}: {item.message}", file=sys.stderr)


def _fail_on_violations(violations: list[ComplianceViolation], *, check_only: bool) -> bool:
    errors = [v for v in violations if v.severity == "error"]
    warns = [v for v in violations if v.severity == "warn"]
    _print_violations(errors + warns)
    if errors:
        return True
    if check_only and warns:
        return True
    return False


def run_compliance(
    config: ProjectConfig,
    *,
    check_only: bool = False,
    sbom_only: bool = False,
    ci_mode: bool = False,
) -> int:
    """Run OSS compliance checks. Returns process exit code."""
    compliance = resolve_compliance_config(config)
    if compliance is None:
        print(
            "compliance: no configuration found — add a `compliance:` block to tooling.yaml "
            "or ensure paths.frontend has pnpm-lock.yaml, yarn.lock, or package-lock.json "
            "for inferred JS-only mode",
            file=sys.stderr,
        )
        return 1

    violations: list[ComplianceViolation] = []
    packages = []

    if compliance.python is not None:
        py_packages, py_violations = scan_python_lockfiles(config, compliance.python.lockfiles)
        packages.extend(py_packages)
        violations.extend(py_violations)

    if compliance.javascript is not None:
        js_packages, js_violations = scan_javascript(config, compliance.javascript)
        packages.extend(js_packages)
        violations.extend(js_violations)

    if not sbom_only:
        violations.extend(check_metadata(config, compliance))
        violations.extend(check_bundled_assets(config, compliance.bundled_assets))
        violations.extend(
            check_license_policy(
                packages,
                forbidden_spdx=compliance.forbidden_spdx,
                warn_spdx=compliance.warn_spdx,
                allow_strong_copyleft=compliance.allow_strong_copyleft,
            )
        )

    sbom_dir = config.repo_root / compliance.outputs.sbom_dir
    notices_path = config.repo_root / compliance.outputs.notices
    sbom_targets: list[tuple[Path, Path]] = []

    with tempfile.TemporaryDirectory(prefix="compliance-sbom-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        gen_sbom_dir = tmp_root if check_only else sbom_dir

        if compliance.python is not None:
            for lockfile_rel in compliance.python.lockfiles:
                stem = Path(lockfile_rel).stem
                parent = Path(lockfile_rel).parent.name
                name = f"{parent}-{stem}" if parent not in {"", "."} else stem
                out = gen_sbom_dir / f"python-{name}.cdx.json"
                violations.extend(write_python_sbom(config, lockfile_rel, out))
                committed = sbom_dir / out.name
                sbom_targets.append((out, committed))

        if compliance.javascript is not None:
            js_out = gen_sbom_dir / "javascript.cdx.json"
            write_javascript_sbom_stub(packages, js_out)
            sbom_targets.append((js_out, sbom_dir / "javascript.cdx.json"))

        merged_generated = gen_sbom_dir / "bom.cdx.json"
        write_merged_sbom([src for src, _ in sbom_targets], merged_generated)
        merged_committed = sbom_dir / "bom.cdx.json"

        notices_text = render_notices(
            packages,
            project_name=config.meta.name,
            project_license=compliance.project_license,
        )

        if check_only:
            violations.extend(
                check_file_drift(notices_text, notices_path, label=compliance.outputs.notices)
            )
            for generated, committed in sbom_targets:
                if generated.is_file():
                    violations.extend(
                        check_file_drift(
                            generated.read_text(encoding="utf-8"),
                            committed,
                            label=str(committed.relative_to(config.repo_root)),
                        )
                    )
            if merged_generated.is_file():
                violations.extend(
                    check_file_drift(
                        merged_generated.read_text(encoding="utf-8"),
                        merged_committed,
                        label=str(merged_committed.relative_to(config.repo_root)),
                    )
                )
        else:
            sbom_dir.mkdir(parents=True, exist_ok=True)
            notices_path.parent.mkdir(parents=True, exist_ok=True)
            notices_path.write_text(notices_text, encoding="utf-8")
            for generated, committed in sbom_targets:
                if generated.is_file():
                    committed.write_text(generated.read_text(encoding="utf-8"), encoding="utf-8")
            if merged_generated.is_file():
                merged_committed.write_text(
                    merged_generated.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            print(f"compliance: wrote {notices_path.relative_to(config.repo_root)}", file=sys.stderr)

    if _fail_on_violations(violations, check_only=check_only):
        return 1
    print("compliance: OK", file=sys.stderr)
    return 0
