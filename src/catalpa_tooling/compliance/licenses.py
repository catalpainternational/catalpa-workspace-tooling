"""Normalize scanner-reported license strings to SPDX-style identifiers."""

from __future__ import annotations

import re

_COMPOUND_SPLIT = re.compile(r"\s*;\s*|\s+AND\s+", re.IGNORECASE)
_SPDX_LIKE = re.compile(r"^[A-Za-z0-9\-.+]+$")


def _canonical_key(value: str) -> str:
    return value.strip().upper().replace(" ", "-")


def normalize_license_token(raw: str) -> str:
    """Map a single license phrase to an SPDX-style identifier when possible."""
    text = raw.strip()
    if not text:
        return "UNKNOWN"

    upper = text.upper()
    if upper in {"UNKNOWN", "N/A"}:
        return "UNKNOWN"
    if upper == "UNLICENSED":
        return "UNLICENSED"

    if _SPDX_LIKE.match(text) and ("-" in text or text in {"MIT", "ISC", "Unlicense"}):
        return text

    lower = text.lower().replace("(", " ").replace(")", " ")

    compact = lower.replace(" ", "")

    if "proprietary" in lower:
        return "LicenseRef-proprietary"
    if "affero" in lower or re.search(r"\bagpl\b", lower):
        return "AGPL-3.0-or-later"
    if "lesser" in lower or "library or lesser" in lower or re.search(r"\blgpl\b", lower):
        if "lgplv3" in compact or re.search(r"v3", compact):
            return "LGPL-3.0-or-later"
        if "lgplv2" in compact or re.search(r"v2", compact):
            return "LGPL-2.0-or-later"
        return "LGPL-2.0-or-later"
    if re.search(r"\bgpl\b", lower) or "general public license" in lower:
        if "gplv3" in compact or re.search(r"v3", compact):
            return "GPL-3.0-only"
        if "or later" in lower:
            return "GPL-2.0-or-later"
        return "GPL-2.0-only"
    if "mozilla public" in lower or "mpl 2" in lower:
        return "MPL-2.0"
    if "apache" in lower:
        return "Apache-2.0"
    if "bsd" in lower:
        if "2-clause" in lower or "2 clause" in lower:
            return "BSD-2-Clause"
        return "BSD-3-Clause"
    if lower in {"mit", "mit license"} or lower.startswith("mit "):
        return "MIT"
    if "isc license" in lower or lower == "isc":
        return "ISC"
    if "unlicense" in lower:
        return "Unlicense"
    if "python software foundation" in lower or lower.startswith("psf"):
        return "PSF-2.0"

    return text


_PERMISSIVE = {
    "MIT",
    "ISC",
    "APACHE-2.0",
    "BSD-2-CLAUSE",
    "BSD-3-CLAUSE",
    "UNLICENSE",
    "PSF-2.0",
}


def _drop_proprietary_scanner_noise(tokens: list[str]) -> list[str]:
    """pip-licenses often adds Other/Proprietary beside a real OSS license."""
    if len(tokens) <= 1:
        return tokens
    if not any(_canonical_key(token) == "LICENSEREF-PROPRIETARY" for token in tokens):
        return tokens
    if any(_canonical_key(token) in _PERMISSIVE for token in tokens):
        return [token for token in tokens if _canonical_key(token) != "LICENSEREF-PROPRIETARY"]
    return tokens


def normalize_license_spdx(raw: str) -> str:
    """Normalize a license field that may contain compound expressions."""
    text = raw.strip()
    if not text:
        return "UNKNOWN"

    parts = [part.strip() for part in _COMPOUND_SPLIT.split(text) if part.strip()]
    if not parts:
        return "UNKNOWN"

    normalized: list[str] = []
    seen: set[str] = set()
    for part in parts:
        token = normalize_license_token(part)
        key = _canonical_key(token)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(token)
    normalized = _drop_proprietary_scanner_noise(normalized)
    return "; ".join(normalized) if normalized else "UNKNOWN"


def license_tokens(license_spdx: str) -> list[str]:
    """Split a (possibly compound) license string into normalized tokens."""
    text = normalize_license_spdx(license_spdx)
    if not text or text.upper() == "UNKNOWN":
        return ["UNKNOWN"]
    return [part.strip() for part in text.split(";") if part.strip()]
