"""Tests for catalpa_tooling.cli_confirm."""

from catalpa_tooling.cli_confirm import confirm_by_typing_env_name


def test_confirm_accepts_exact_env_name(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda: "staging")
    assert confirm_by_typing_env_name("staging") is True


def test_confirm_rejects_wrong_text(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda: "yes")
    assert confirm_by_typing_env_name("staging") is False


def test_confirm_rejects_empty(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda: "")
    assert confirm_by_typing_env_name("staging") is False


def test_confirm_accepts_stripped_match(monkeypatch) -> None:
    monkeypatch.setattr("builtins.input", lambda: "  demo  ")
    assert confirm_by_typing_env_name("demo") is True
