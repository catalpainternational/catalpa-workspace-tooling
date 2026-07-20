"""Version parsing and bump helpers for ``dk cut-release``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

BumpKind = Literal["major", "minor", "hotfix"]

_DEV_BRANCH_RE = re.compile(
    r"^dev-(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?$"
)
_V_TAG_RE = re.compile(
    r"^v(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?$"
)
_BETA_TAG_RE = re.compile(
    r"^v(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?\.beta\.(?P<w>\d+)$"
)


@dataclass(frozen=True, slots=True)
class Version:
    major: int
    minor: int
    patch: int = 0

    def bump(self, kind: BumpKind) -> Version:
        if kind == "major":
            return Version(self.major + 1, 0, 0)
        if kind == "minor":
            return Version(self.major, self.minor + 1, 0)
        if kind == "hotfix":
            return Version(self.major, self.minor, self.patch + 1)
        raise ValueError(f"unknown bump kind: {kind}")


def format_omit_zeros(version: Version) -> str:
    """Format ``7.5``, ``8.0``, or ``7.4.1`` (omit trailing patch zero)."""
    if version.patch == 0:
        return f"{version.major}.{version.minor}"
    return f"{version.major}.{version.minor}.{version.patch}"


def format_dev_branch(version: Version) -> str:
    return f"dev-{format_omit_zeros(version)}"


def format_v_tag(version: Version) -> str:
    return f"v{format_omit_zeros(version)}"


def format_beta_tag(version: Version, w: int) -> str:
    if w < 1:
        raise ValueError(f"beta W must be >= 1, got {w}")
    return f"{format_v_tag(version)}.beta.{w}"


def parse_dev_branch(name: str) -> Version | None:
    m = _DEV_BRANCH_RE.fullmatch(name.strip())
    if not m:
        return None
    patch = m.group("patch")
    return Version(int(m.group("major")), int(m.group("minor")), int(patch) if patch else 0)


def parse_v_tag(name: str) -> Version | None:
    """Parse a final release tag ``vX.Y`` / ``vX.Y.Z`` (not beta)."""
    m = _V_TAG_RE.fullmatch(name.strip())
    if not m:
        return None
    patch = m.group("patch")
    return Version(int(m.group("major")), int(m.group("minor")), int(patch) if patch else 0)


def parse_beta_tag(name: str) -> tuple[Version, int] | None:
    m = _BETA_TAG_RE.fullmatch(name.strip())
    if not m:
        return None
    patch = m.group("patch")
    version = Version(int(m.group("major")), int(m.group("minor")), int(patch) if patch else 0)
    return version, int(m.group("w"))


def next_beta_w(existing_tags: list[str], version: Version) -> int:
    """Return the next ``W`` for ``vX.Y.Z.beta.W`` given existing tag names."""
    prefix = f"{format_v_tag(version)}.beta."
    max_w = 0
    for tag in existing_tags:
        parsed = parse_beta_tag(tag)
        if parsed is None:
            continue
        ver, w = parsed
        if ver == version:
            max_w = max(max_w, w)
        elif tag.startswith(prefix):
            # Defensive: same string prefix without dataclass equality edge cases
            try:
                max_w = max(max_w, int(tag[len(prefix) :]))
            except ValueError:
                continue
    return max_w + 1
