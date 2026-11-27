"""How a rendered host gets its private images: a login for each pull, or a stored one.

A host pulls when dk tells it to: ``host provision`` (every image, before anything starts or is
restored) and a bare ``dk <env>`` (a deploy). The units use ``Pull=missing``, so a reboot or an
OS update pulls nothing. Only ``fcos.auto_update`` pulls with nobody there (``podman
auto-update``), so only an env with it keeps a registry login on the host (``registry_user`` /
``registry_token`` in sops, a bot's token: ``dk <env> host login``).

Every other env keeps none. For each pull dk sends a token over SSH's stdin; the host logs in to
an auth file under ``/run/user/<uid>`` (memory), pulls, and removes it when the script ends, also
when a pull fails. The token comes, in order, from:

1. ``DK_REGISTRY_TOKEN`` (user ``DK_REGISTRY_USER``, else ``GITHUB_ACTOR``): CI, e.g. a
   workflow's ``GITHUB_TOKEN`` with ``packages: read``;
2. ``registry_user`` / ``registry_token`` in sops, when the project keeps one;
3. ``gh auth token``, for ghcr.io, when its scopes include ``read:packages``;
4. a prompt, on a terminal.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

TOKEN_ENV = "DK_REGISTRY_TOKEN"
USER_ENV = "DK_REGISTRY_USER"
GHCR = "ghcr.io"


class RegistryLoginError(RuntimeError):
    pass


@dataclass(frozen=True)
class PullLogin:
    user: str
    token: str
    source: str  # said to the operator; never the token


def keeps_login(c) -> bool:
    """Whether the host keeps a registry login: only for ``podman auto-update``."""
    return bool(c.manifest.get("auto_update"))


def pull_login(
    registries: list[str], creds: dict | Callable[[], dict], *, env_name: str, interactive: bool | None = None
) -> PullLogin | None:
    """A login for one pull from ``registries`` (None when every image is public). ``creds``: the
    env's credentials, or a function that reads them (only called when the token isn't set)."""
    if not registries:
        return None
    token = (os.environ.get(TOKEN_ENV) or "").strip()
    if token:
        user = (os.environ.get(USER_ENV) or os.environ.get("GITHUB_ACTOR") or "").strip()
        if not user:
            raise RegistryLoginError(f"{TOKEN_ENV} is set: set {USER_ENV} too (the token's user)")
        return PullLogin(user, token, TOKEN_ENV)
    from catalpa_tooling.project_credentials import registry_login

    stored = registry_login(creds() if callable(creds) else creds)
    if stored:
        return PullLogin(*stored, "registry_token in sops")
    if registries == [GHCR]:
        gh = gh_login()
        if gh:
            return gh
    if interactive is None:
        interactive = sys.stdin.isatty()
    if interactive:
        return _prompt(registries)
    raise RegistryLoginError(
        f"pulling from {', '.join(registries)} needs a token for this pull (the host keeps none): "
        f"set {TOKEN_ENV} and {USER_ENV}, or `gh auth refresh -s read:packages`, or run it on a "
        f"terminal to be asked. dk {env_name} sends it over SSH's stdin; it's gone when the pull ends."
    )


def _gh(*args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def gh_login() -> PullLogin | None:
    """The GitHub CLI's token, when its scopes let it pull packages. Warns when they're wider."""
    r = _gh("api", "--include", "user")
    if r is None or r.returncode != 0:
        return None
    head, _, body = r.stdout.partition("\r\n\r\n")
    if not body:
        head, _, body = r.stdout.partition("\n\n")
    scopes: set[str] = set()
    for line in head.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "x-oauth-scopes":
            scopes = {s.strip() for s in value.split(",") if s.strip()}
    if not scopes & {"read:packages", "write:packages"}:
        print(
            "registry: gh's token can't pull packages: `gh auth refresh -s read:packages` lets dk "
            "use it.",
            file=sys.stderr,
        )
        return None
    try:
        user = str(json.loads(body)["login"])
    except (ValueError, KeyError, TypeError):
        return None
    t = _gh("auth", "token")
    token = (t.stdout.strip() if t and t.returncode == 0 else "")
    if not token:
        return None
    if scopes & {"repo", "admin:org", "delete_repo", "workflow"}:
        print(
            f"registry: gh's token also has {', '.join(sorted(scopes - {'read:packages', 'write:packages'}))}; "
            f"it's on the host only for the pull. For a token that can only pull, set {TOKEN_ENV} "
            f"(a classic token with just read:packages) and {USER_ENV}.",
            file=sys.stderr,
        )
    return PullLogin(user, token, "gh auth token")


def _prompt(registries: list[str]) -> PullLogin:
    import getpass

    where = ", ".join(registries)
    print(
        f"Pulling from {where}: a token for this pull only (the host keeps none). GitHub: a classic "
        "token with read:packages.",
        file=sys.stderr,
    )
    user = input(f"{where} user: ").strip()
    token = getpass.getpass(f"{where} token: ").strip()
    if not user or not token:
        raise RegistryLoginError("no user or token given")
    return PullLogin(user, token, "typed in")


def login_lines(user: str, registries: list[str]) -> list[str]:
    """Shell lines that log in to a temporary auth file with the token from stdin, and remove it
    when the script ends. ``$AUTH`` is then the ``--authfile`` option for ``podman pull``."""
    lines = [
        'AUTHFILE="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/dk-pull-auth.json"',
        "trap 'rm -f \"$AUTHFILE\"' EXIT",
        "umask 077",
        "IFS= read -r TOKEN || true",
    ]
    for registry in registries:
        lines.append(
            f'printf %s "$TOKEN" | podman login --authfile "$AUTHFILE" --username {shlex.quote(user)} '
            f"--password-stdin {shlex.quote(registry)} >/dev/null"
        )
    lines += ["TOKEN=", 'AUTH="--authfile $AUTHFILE"']
    return lines
