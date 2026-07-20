"""Tests for cut_release_version helpers."""

from __future__ import annotations

import pytest

from catalpa_tooling.cut_release_version import (
    Version,
    format_beta_tag,
    format_dev_branch,
    format_omit_zeros,
    format_v_tag,
    next_beta_w,
    parse_beta_tag,
    parse_dev_branch,
    parse_v_tag,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("dev-7.4", Version(7, 4, 0)),
        ("dev-7.4.1", Version(7, 4, 1)),
        ("dev-8.0", Version(8, 0, 0)),
        ("dev-2.9", Version(2, 9, 0)),
        ("main", None),
        ("dev-7.4.1.beta.1", None),
        ("feature/foo", None),
    ],
)
def test_parse_dev_branch(name: str, expected: Version | None) -> None:
    assert parse_dev_branch(name) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("v7.4", Version(7, 4, 0)),
        ("v7.4.1", Version(7, 4, 1)),
        ("v2.9", Version(2, 9, 0)),
        ("v7.4.1.beta.1", None),
        ("v2.8.2a", None),
        ("v2.4-beta", None),
    ],
)
def test_parse_v_tag(name: str, expected: Version | None) -> None:
    assert parse_v_tag(name) == expected


def test_parse_beta_tag() -> None:
    assert parse_beta_tag("v7.4.1.beta.2") == (Version(7, 4, 1), 2)
    assert parse_beta_tag("v2.9.beta.1") == (Version(2, 9, 0), 1)
    assert parse_beta_tag("v7.4.1") is None


def test_format_omit_zeros_and_tags() -> None:
    assert format_omit_zeros(Version(7, 5, 0)) == "7.5"
    assert format_omit_zeros(Version(8, 0, 0)) == "8.0"
    assert format_omit_zeros(Version(7, 4, 1)) == "7.4.1"
    assert format_dev_branch(Version(7, 5, 0)) == "dev-7.5"
    assert format_v_tag(Version(7, 4, 1)) == "v7.4.1"
    assert format_beta_tag(Version(7, 4, 1), 3) == "v7.4.1.beta.3"


def test_bump_kinds() -> None:
    v = Version(7, 4, 1)
    assert v.bump("major") == Version(8, 0, 0)
    assert v.bump("minor") == Version(7, 5, 0)
    assert v.bump("hotfix") == Version(7, 4, 2)
    assert format_dev_branch(v.bump("major")) == "dev-8.0"
    assert format_dev_branch(v.bump("minor")) == "dev-7.5"
    assert format_dev_branch(v.bump("hotfix")) == "dev-7.4.2"


def test_next_beta_w() -> None:
    tags = ["v7.4.1", "v7.4.1.beta.1", "v7.4.1.beta.3", "v7.4.0.beta.9", "v2.9.beta.1"]
    assert next_beta_w(tags, Version(7, 4, 1)) == 4
    assert next_beta_w([], Version(7, 4, 1)) == 1
    assert next_beta_w(["v7.4.beta.2"], Version(7, 4, 0)) == 3
