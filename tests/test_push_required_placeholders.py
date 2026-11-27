"""#211: `dk push` fills unset ``${KEY:?…}`` variables for the build, but not ones used in build.args."""

import json
import unittest
from unittest.mock import MagicMock, patch

from catalpa_tooling.build_push import BUILD_PLACEHOLDER, fill_required_placeholders


def _missing(name: str) -> MagicMock:
    return MagicMock(
        returncode=1,
        stdout="",
        stderr=f"error while interpolating services.web.environment.{name}: "
        f"required variable {name} is missing a value: {name} must be set",
    )


def _ok(services: dict) -> MagicMock:
    return MagicMock(returncode=0, stdout=json.dumps({"services": services}), stderr="")


class TestFillRequiredPlaceholders(unittest.TestCase):
    def setUp(self) -> None:
        self.config = MagicMock()
        self.config.compose_prod = "compose.yml"

    def _run(self, results: list[MagicMock], env: dict[str, str]) -> bool:
        with patch("catalpa_tooling.build_push.run_cmd", side_effect=results):
            return fill_required_placeholders(self.config, env)

    def test_fills_each_missing_variable(self) -> None:
        env: dict[str, str] = {}
        ok = self._run([_missing("A"), _missing("B"), _ok({"web": {}})], env)
        self.assertTrue(ok)
        self.assertEqual(env, {"A": BUILD_PLACEHOLDER, "B": BUILD_PLACEHOLDER})

    def test_nothing_missing_changes_nothing(self) -> None:
        env = {"X": "1"}
        self.assertTrue(self._run([_ok({})], env))
        self.assertEqual(env, {"X": "1"})

    def test_build_arg_is_refused_naming_it(self) -> None:
        env: dict[str, str] = {}
        svc = {"web": {"build": {"args": {"TOKEN": BUILD_PLACEHOLDER}}}}
        with patch("sys.stderr") as err:
            ok = self._run([_missing("TOKEN"), _ok(svc)], env)
        self.assertFalse(ok)
        printed = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("web: build arg TOKEN", printed)

    def test_other_compose_errors_are_left_to_the_build(self) -> None:
        other = MagicMock(returncode=1, stdout="", stderr="yaml: line 3: bad")
        env: dict[str, str] = {}
        self.assertTrue(self._run([other], env))
        self.assertEqual(env, {})


if __name__ == "__main__":
    unittest.main()
