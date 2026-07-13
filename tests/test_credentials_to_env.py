"""Tests for catalpa_tooling._credentials_to_env (run: ``uv run tests workspace`` from repo root)."""

import unittest

from catalpa_tooling import _credentials_to_env


class TestCredentialsToEnv(unittest.TestCase):
    def test_skips_sops_and_nested(self) -> None:
        out = _credentials_to_env(
            {
                "sops": {"age": []},
                "django_secret_key": "x",
                "nested": {"a": 1},
                "a_list": [1, 2],
            }
        )
        self.assertEqual(out, {"DJANGO_SECRET_KEY": "x"})

    def test_uppercases_keys_and_none(self) -> None:
        out = _credentials_to_env(
            {
                "FOO": "bar",
                "empty": None,
                "num": 42,
            }
        )
        self.assertEqual(
            out,
            {"FOO": "bar", "EMPTY": "", "NUM": "42"},
        )


if __name__ == "__main__":
    unittest.main()
