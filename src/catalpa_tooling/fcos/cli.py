"""``dk <env> fcos render|check``."""

from __future__ import annotations

import argparse
import sys

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.fcos.render import (
    RenderError,
    compare_render,
    generated_dir,
    render_env,
    required_secret_problems,
    write_render,
)


def attach_fcos_parser(cmd_sub: argparse._SubParsersAction) -> None:
    p = cmd_sub.add_parser(
        "fcos",
        help="Fedora CoreOS host files (rootless quadlets) rendered from the compose file.",
    )
    sub = p.add_subparsers(dest="fcos_command", required=True)
    sub.add_parser(
        "render",
        help="Write docker/envs/<env>/generated/fcos/ from compose + info.yaml (commit the result).",
    )
    sub.add_parser(
        "check",
        help=(
            "Fail if the committed render differs from a fresh one, or if plain compose would "
            "start without a credential."
        ),
    )


def handle_fcos_command(ns: argparse.Namespace, config: ProjectConfig, env_name: str) -> int:
    command = ns.fcos_command
    gen_dir = generated_dir(config, env_name)
    rel = gen_dir.relative_to(config.repo_root)
    try:
        render = render_env(config, env_name)
    except RenderError as exc:
        print(f"fcos {command}: {exc}", file=sys.stderr)
        return 1

    if command == "render":
        if getattr(ns, "dry_run", False):
            for line in compare_render(gen_dir, render.files) or ["(no changes)"]:
                print(line)
            return 0
        changed = write_render(gen_dir, render.files)
        for line in changed:
            print(line)
        print(
            f"{rel}: {len(render.files)} files, {len(changed)} changed. Commit them; "
            f"`dk {env_name} fcos check` compares them with a fresh render.",
            file=sys.stderr,
        )
        problems = required_secret_problems(config, render)
        for p in problems:
            print(f"warning: {p}", file=sys.stderr)
        return 0

    rc = 0
    diffs = compare_render(gen_dir, render.files)
    if diffs:
        print(
            f"{rel} differs from a fresh render (run `dk {env_name} fcos render` and commit):",
            file=sys.stderr,
        )
        for line in diffs:
            print(f"  {line}", file=sys.stderr)
        rc = 1
    problems = required_secret_problems(config, render)
    if problems:
        print("credentials that plain `docker compose up` would run without:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        rc = 1
    if rc == 0:
        print(f"{rel}: matches a fresh render; every credential is required.", file=sys.stderr)
    return rc
