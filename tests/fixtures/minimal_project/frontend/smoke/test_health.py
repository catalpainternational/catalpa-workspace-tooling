"""HTTP smoke check (no browser) — reference for docs/SMOKE_TESTS.md."""

from __future__ import annotations

import urllib.error
import urllib.request


def test_frontend_root_responds(fe_url: str) -> None:
    with urllib.request.urlopen(f"{fe_url}/", timeout=15) as resp:
        assert 200 <= resp.status < 400
