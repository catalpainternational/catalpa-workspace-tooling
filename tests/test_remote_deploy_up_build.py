"""Tests for ``_insert_up_build_if_no_registry`` (run: uv run python -m unittest discover -s tests -v)."""

import unittest

from catalpa_tooling.managed_deploy_env import (
    _effective_deploy_image_tag,
    _info_image_tag,
)
from catalpa_tooling.remote_deploy import (
    _insert_up_build_if_no_registry,
    _insert_up_prepulled_pull_flags,
    _normalize_dk_env_argv,
)


class TestInsertUpPrepulledPullFlags(unittest.TestCase):
    def test_inserts_before_service_name(self) -> None:
        self.assertEqual(
            _insert_up_prepulled_pull_flags(
                ["up", "-d", "db"],
                use_prepulled_registry=True,
            ),
            ["up", "-d", "--pull", "missing", "--no-build", "db"],
        )

    def test_skips_when_not_prepulled(self) -> None:
        self.assertEqual(
            _insert_up_prepulled_pull_flags(
                ["up", "-d", "db"],
                use_prepulled_registry=False,
            ),
            ["up", "-d", "db"],
        )

    def test_respects_existing_pull_and_build_flags(self) -> None:
        self.assertEqual(
            _insert_up_prepulled_pull_flags(
                ["up", "-d", "--pull", "always", "--build", "django"],
                use_prepulled_registry=True,
            ),
            ["up", "-d", "--pull", "always", "--build", "django"],
        )

    def test_non_up_unchanged(self) -> None:
        self.assertEqual(
            _insert_up_prepulled_pull_flags(
                ["logs", "-f", "db"],
                use_prepulled_registry=True,
            ),
            ["logs", "-f", "db"],
        )


class TestInsertUpBuildIfNoRegistry(unittest.TestCase):
    def test_skips_when_prepulled_registry(self) -> None:
        self.assertEqual(
            _insert_up_build_if_no_registry(["up", "-d"], use_prepulled_registry=True),
            ["up", "-d"],
        )

    def test_inserts_after_flags(self) -> None:
        self.assertEqual(
            _insert_up_build_if_no_registry(["up", "-d"], use_prepulled_registry=False),
            ["up", "-d", "--build"],
        )

    def test_inserts_before_service_name(self) -> None:
        self.assertEqual(
            _insert_up_build_if_no_registry(["up", "-d", "django"], use_prepulled_registry=False),
            ["up", "-d", "--build", "django"],
        )

    def test_pull_always_value_skipped(self) -> None:
        self.assertEqual(
            _insert_up_build_if_no_registry(
                ["up", "--pull", "always", "-d"],
                use_prepulled_registry=False,
            ),
            ["up", "--pull", "always", "-d", "--build"],
        )

    def test_respects_existing_build_flags(self) -> None:
        self.assertEqual(
            _insert_up_build_if_no_registry(["up", "-d", "--build"], use_prepulled_registry=False),
            ["up", "-d", "--build"],
        )
        self.assertEqual(
            _insert_up_build_if_no_registry(["up", "-d", "--no-build"], use_prepulled_registry=False),
            ["up", "-d", "--no-build"],
        )

    def test_non_up_unchanged(self) -> None:
        self.assertEqual(
            _insert_up_build_if_no_registry(["logs", "-f"], use_prepulled_registry=False),
            ["logs", "-f"],
        )


class TestNormalizeDkEnvArgv(unittest.TestCase):
    def test_env_tag_reordered_before_compose(self) -> None:
        self.assertEqual(
            _normalize_dk_env_argv(["staging", "--tag", "v9", "up", "-d"]),
            ["--tag", "v9", "staging", "up", "-d"],
        )

    def test_env_tag_equals_form(self) -> None:
        self.assertEqual(
            _normalize_dk_env_argv(["local", "--tag=v2", "ps"]),
            ["--tag", "v2", "local", "ps"],
        )

    def test_dry_run_then_env_then_tag(self) -> None:
        self.assertEqual(
            _normalize_dk_env_argv(["demo", "--dry-run", "--tag", "rc1"]),
            ["--dry-run", "--tag", "rc1", "demo"],
        )

    def test_yes_trailing_with_tag_after_env(self) -> None:
        self.assertEqual(
            _normalize_dk_env_argv(["demo", "--tag", "t1", "wipe", "--yes"]),
            ["--yes", "--tag", "t1", "demo", "wipe"],
        )


class TestEffectiveDeployImageTag(unittest.TestCase):
    def test_cli_overrides_yaml(self) -> None:
        self.assertEqual(
            _effective_deploy_image_tag({"image_tag": "from-yaml"}, "from-cli"),
            "from-cli",
        )

    def test_yaml_when_no_cli(self) -> None:
        self.assertEqual(
            _effective_deploy_image_tag({"image_tag": "pinned"}, None),
            "pinned",
        )

    def test_empty_cli_falls_back_to_yaml(self) -> None:
        self.assertEqual(
            _effective_deploy_image_tag({"image_tag": "pinned"}, "  "),
            "pinned",
        )

    def test_cli_when_yaml_missing(self) -> None:
        self.assertEqual(_effective_deploy_image_tag({}, "only-cli"), "only-cli")


class TestInfoImageTag(unittest.TestCase):
    def test_missing_key(self) -> None:
        self.assertIsNone(_info_image_tag({}))

    def test_empty_or_null(self) -> None:
        self.assertIsNone(_info_image_tag({"image_tag": ""}))
        self.assertIsNone(_info_image_tag({"image_tag": None}))

    def test_non_empty(self) -> None:
        self.assertEqual(_info_image_tag({"image_tag": " v1 "}), "v1")
        self.assertEqual(_info_image_tag({"image_tag": "latest"}), "latest")


if __name__ == "__main__":
    unittest.main()
