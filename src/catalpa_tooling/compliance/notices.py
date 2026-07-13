"""Generate THIRD_PARTY_NOTICES.md from scan results."""

from __future__ import annotations

from catalpa_tooling.compliance.types import CompliancePackage

_SECTION_ORDER = (
    ("javascript", "JavaScript"),
    ("python", "Python"),
    ("other", "Other"),
)


def _section_key(source: str) -> str:
    if source.startswith("javascript:"):
        return "javascript"
    if source.startswith("python:"):
        return "python"
    return "other"


def _render_table(
    packages: list[CompliancePackage],
    *,
    include_source: bool,
) -> list[str]:
    if not packages:
        return ["_No packages._", ""]
    lines = []
    if include_source:
        lines.extend(["| Name | Version | License | Source |", "| --- | --- | --- | --- |"])
    else:
        lines.extend(["| Name | Version | License |", "| --- | --- | --- |"])
    for pkg in sorted(packages, key=lambda p: p.name.lower()):
        version = pkg.version or "—"
        license_cell = pkg.license_spdx.replace("|", "\\|")
        if include_source:
            lines.append(f"| {pkg.name} | {version} | {license_cell} | {pkg.source} |")
        else:
            lines.append(f"| {pkg.name} | {version} | {license_cell} |")
    lines.append("")
    return lines


def render_notices(
    packages: list[CompliancePackage],
    *,
    project_name: str,
    project_license: str,
) -> str:
    grouped: dict[str, list[CompliancePackage]] = {
        "javascript": [],
        "python": [],
        "other": [],
    }
    for pkg in packages:
        grouped[_section_key(pkg.source)].append(pkg)

    lines = [
        "# Third-party notices",
        "",
        f"Generated for **{project_name}** by `uv run test compliance`.",
        f"Project license: **{project_license}**.",
        "",
        "This file lists production dependencies and their declared licenses.",
        "Review manually after the first baseline; commit updates with dependency bumps.",
        "",
    ]

    for key, title in _SECTION_ORDER:
        section_packages = grouped[key]
        if not section_packages:
            continue
        lines.append(f"## {title}")
        lines.append("")
        include_source = len({pkg.source for pkg in section_packages}) > 1
        lines.extend(_render_table(section_packages, include_source=include_source))

    return "\n".join(lines).rstrip() + "\n"
