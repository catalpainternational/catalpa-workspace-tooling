"""Optional shell completion via argcomplete."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


def activate(parser: argparse.ArgumentParser) -> None:
    """Register ``parser`` with argcomplete when installed."""
    try:
        import argcomplete
    except ImportError:
        return
    argcomplete.autocomplete(parser)


def argcomplete_active() -> bool:
    """True when the shell is probing this process for tab completions."""
    import os

    return bool(os.environ.get("_ARGCOMPLETE") or os.environ.get("_ARGCOMPLETE_COMPAT"))


def choices_completer(choices: Iterable[str]):
    """Return an argcomplete ``ChoicesCompleter`` or ``None`` when argcomplete is absent."""
    try:
        from argcomplete.completers import ChoicesCompleter
    except ImportError:
        return None
    return ChoicesCompleter(list(choices))


def attach_choices_completer(action: argparse.Action, choices: Iterable[str]) -> None:
    """Attach dynamic tab completion to an ``ArgumentParser`` action."""
    completer = choices_completer(choices)
    if completer is not None:
        action.completer = completer
