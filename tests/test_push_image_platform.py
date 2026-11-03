"""#114: `dk push` must not ship images whose architecture is not what compose declared."""

import unittest
from unittest.mock import MagicMock, patch

from catalpa_tooling.build_push import verify_built_image_platforms


def _svc(image: str, platform: str | None = None, *, build: bool = True) -> dict:
    svc: dict = {"image": image}
    if build:
        svc["build"] = {"context": "."}
    if platform is not None:
        svc["platform"] = platform
    return svc


class TestVerifyBuiltImagePlatforms(unittest.TestCase):
    def setUp(self) -> None:
        self.config = MagicMock()
        self.config.compose_prod = "compose.yml"

    @staticmethod
    def _inspect(platform: str, returncode: int = 0) -> MagicMock:
        return MagicMock(returncode=returncode, stdout=platform, stderr="")

    def _run(self, services: dict, inspects: list[MagicMock]) -> int:
        with (
            patch(
                "catalpa_tooling.stale_stack._compose_config",
                return_value={"services": services},
            ),
            patch("catalpa_tooling.build_push.run_cmd", side_effect=inspects),
        ):
            return verify_built_image_platforms(self.config, {})

    def test_matching_platform_passes(self) -> None:
        rc = self._run(
            {"db": _svc("ghcr.io/x/db:t", "linux/amd64")},
            [self._inspect("linux/amd64\n")],
        )
        self.assertEqual(rc, 0)

    def test_mismatch_blocks_the_push(self) -> None:
        """The Podman case: compose asked amd64, the engine built arm64, push must stop."""
        rc = self._run(
            {"db": _svc("ghcr.io/x/db:t", "linux/amd64")},
            [self._inspect("linux/arm64\n")],
        )
        self.assertEqual(rc, 1)

    def test_one_mismatch_among_several_blocks(self) -> None:
        rc = self._run(
            {
                "db": _svc("ghcr.io/x/db:t", "linux/amd64"),
                "web": _svc("ghcr.io/x/web:t", "linux/amd64"),
                "proxy": _svc("ghcr.io/x/proxy:t", "linux/amd64"),
            },
            [
                self._inspect("linux/amd64\n"),
                self._inspect("linux/amd64\n"),
                self._inspect("linux/arm64\n"),
            ],
        )
        self.assertEqual(rc, 1)

    def test_undeclared_platform_warns_but_allows(self) -> None:
        """An unpinned service is a warning, not a block — some projects build native on purpose."""
        rc = self._run(
            {"db": _svc("ghcr.io/x/db:t", None)},
            [self._inspect("linux/arm64\n")],
        )
        self.assertEqual(rc, 0)

    def test_uninspectable_image_does_not_block(self) -> None:
        """compose_yml_build already failed loudly if the build broke; do not double-report."""
        rc = self._run(
            {"db": _svc("ghcr.io/x/db:t", "linux/amd64")},
            [self._inspect("", returncode=1)],
        )
        self.assertEqual(rc, 0)

    def test_services_without_build_are_ignored(self) -> None:
        """Pulled sidecars (redis, mailpit) are not ours to police."""
        with (
            patch(
                "catalpa_tooling.stale_stack._compose_config",
                return_value={"services": {"redis": _svc("redis:7", "linux/amd64", build=False)}},
            ),
            patch("catalpa_tooling.build_push.run_cmd") as mock_run,
        ):
            self.assertEqual(verify_built_image_platforms(self.config, {}), 0)
            mock_run.assert_not_called()

    def test_unresolvable_compose_config_does_not_block(self) -> None:
        with patch("catalpa_tooling.stale_stack._compose_config", return_value={}):
            self.assertEqual(verify_built_image_platforms(self.config, {}), 0)

    def test_malformed_inspect_output_is_ignored(self) -> None:
        rc = self._run(
            {"db": _svc("ghcr.io/x/db:t", "linux/amd64")},
            [self._inspect("garbage\n")],
        )
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
