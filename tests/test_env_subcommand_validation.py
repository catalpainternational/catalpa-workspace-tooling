"""#85: an unknown `dk <env>` subcommand must be refused, not run as `up -d`."""

import unittest

from catalpa_tooling.cli.dk_argv import (
    COMPOSE_VERBS,
    SPECIAL_ENV_COMMANDS,
    is_implicit_compose_argv,
    unknown_env_subcommand,
    unknown_env_subcommand_message,
)


class TestImplicitComposePassthrough(unittest.TestCase):
    def test_bare_env_is_passthrough(self) -> None:
        """`dk dev` is the documented `up -d` shorthand and must keep working."""
        self.assertTrue(is_implicit_compose_argv(["dev"]))

    def test_compose_verbs_are_passthrough(self) -> None:
        for verb in ("up", "down", "ps", "logs", "build", "config", "exec", "restart"):
            with self.subTest(verb=verb):
                self.assertTrue(is_implicit_compose_argv(["dev", verb]))

    def test_every_listed_compose_verb_is_passthrough(self) -> None:
        for verb in sorted(COMPOSE_VERBS):
            with self.subTest(verb=verb):
                self.assertTrue(is_implicit_compose_argv(["dev", verb]))

    def test_unknown_subcommand_is_not_passthrough(self) -> None:
        """The reported case: `context` is a `dk worktree` subcommand, not an env one."""
        self.assertFalse(is_implicit_compose_argv(["dev", "context", "--json"]))

    def test_typo_of_a_real_verb_is_not_passthrough(self) -> None:
        self.assertFalse(is_implicit_compose_argv(["dev", "logz"]))
        self.assertFalse(is_implicit_compose_argv(["dev", "ups"]))

    def test_special_commands_are_not_passthrough(self) -> None:
        for name in ("manage", "docker", "info", "wipe"):
            with self.subTest(name=name):
                self.assertFalse(is_implicit_compose_argv(["dev", name]))


class TestUnknownEnvSubcommand(unittest.TestCase):
    def test_reports_the_offending_token(self) -> None:
        self.assertEqual(unknown_env_subcommand(["dev", "context", "--json"]), "context")

    def test_none_for_compose_verbs(self) -> None:
        for verb in sorted(COMPOSE_VERBS):
            with self.subTest(verb=verb):
                self.assertIsNone(unknown_env_subcommand(["dev", verb]))

    def test_none_for_special_commands(self) -> None:
        for name in sorted(SPECIAL_ENV_COMMANDS):
            with self.subTest(name=name):
                self.assertIsNone(unknown_env_subcommand(["dev", name]))

    def test_none_for_bare_env_and_flags(self) -> None:
        self.assertIsNone(unknown_env_subcommand(["dev"]))
        self.assertIsNone(unknown_env_subcommand(["dev", "--dry-run"]))
        self.assertIsNone(unknown_env_subcommand(["dev", "--help"]))

    def test_message_names_both_sets_and_explains_the_danger(self) -> None:
        msg = unknown_env_subcommand_message("dev", "context")
        self.assertIn("unknown subcommand 'context'", msg)
        # Both vocabularies, because argparse alone would list only its own choices.
        self.assertIn("up", msg)
        self.assertIn("logs", msg)
        self.assertIn("manage", msg)
        self.assertIn("up -d", msg)

    def test_special_and_compose_names_do_not_overlap(self) -> None:
        """An overlap would make the two lists contradict each other."""
        self.assertEqual(SPECIAL_ENV_COMMANDS & COMPOSE_VERBS, frozenset())


if __name__ == "__main__":
    unittest.main()
