"""Project-level credentials: ``credentials.yaml`` beside ``envs/`` (``docker/credentials.yaml``).

Values every env of a project shares, such as the registry bot's token, so they're kept once and
can have their own ``.sops.yaml`` recipients (often fewer people than the env files). An env's
own ``credentials.yaml`` overrides a project value of the same name.

Read by the rendered-host commands (``runtime: quadlet``): the render sees both files' names,
and ``secrets push``, ``host provision`` and ``host login`` read both files' values. Compose envs
don't read it yet (tooling 2.0, M7).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from catalpa_tooling.config import ProjectConfig

#: The registry bot's login, used by ``host provision`` and ``host login``.
REGISTRY_USER = "registry_user"
REGISTRY_TOKEN = "registry_token"


def project_credentials_path(config: ProjectConfig) -> Path:
    return config.deploy_envs_dir.parent / "credentials.yaml"


def env_credentials_path(config: ProjectConfig, env_name: str) -> Path:
    return config.deploy_envs_dir / env_name / "credentials.yaml"


def credentials_for_env(config: ProjectConfig, env_name: str, *, decrypt=None) -> dict:
    """The project's credentials, then the env's on top (decrypted with sops)."""
    if decrypt is None:
        from catalpa_tooling.sops_credentials import decrypt_credentials_yaml as decrypt

    out: dict = {}
    for path in (project_credentials_path(config), env_credentials_path(config, env_name)):
        if path.is_file():
            out.update({k: v for k, v in decrypt(path).items() if k != "sops"})
    return out


def registry_login(credentials: dict) -> tuple[str, str] | None:
    """(user, token) when both are set, else None."""
    flat = {str(k).lower(): v for k, v in credentials.items()}
    user, token = flat.get(REGISTRY_USER), flat.get(REGISTRY_TOKEN)
    if user and token:
        return str(user), str(token)
    return None


def cmd_project_secrets(config: ProjectConfig, *, dry_run: bool) -> int:
    """``dk secrets``: edit the project's ``credentials.yaml`` with sops (creates it)."""
    path = project_credentials_path(config)
    repo_root = config.repo_root
    rel = str(path.relative_to(repo_root))
    if dry_run:
        print(f"dry-run: would run sops {rel} (cwd {repo_root})", file=sys.stderr)
        return 0
    existed = path.is_file()
    rc = subprocess.run(["sops", rel], cwd=repo_root, check=False).returncode
    if rc != 0 and not existed and not path.is_file():
        print(
            f"dk secrets: sops couldn't create {rel}. It needs a creation rule in .sops.yaml, "
            "with the people who may read it:\n"
            "  creation_rules:\n"
            f"    - path_regex: {_regex(rel)}\n"
            "      age: >-\n"
            "        <their public keys>\n"
            "Put it before a rule whose path_regex would also match.",
            file=sys.stderr,
        )
    return rc


def _regex(rel: str) -> str:
    return "^" + rel.replace(".", r"\.") + "$"
