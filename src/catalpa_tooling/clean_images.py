"""dk clean-images: remove old GHCR package versions using project retention rules."""

from __future__ import annotations

import sys

from catalpa_tooling.cli_confirm import confirm_yes_default_no
from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.ghcr_cleanup import resolve_ghcr_cleanup_plan, run_cleanup


def clean_images(
    config: ProjectConfig,
    *,
    apply: bool = False,
    yes: bool = False,
    keep_n_tagged: int | None = None,
    older_than: str | None = None,
    delete_untagged: bool | None = None,
    package: str | None = None,
    token: str | None = None,
) -> int:
    try:
        packages = (package,) if package else None
        plan = resolve_ghcr_cleanup_plan(
            config,
            keep_n_tagged=keep_n_tagged,
            older_than=older_than,
            delete_untagged=delete_untagged,
            packages=packages,
        )
    except ValueError as exc:
        print(f"clean-images: {exc}", file=sys.stderr)
        return 1

    dry_run = not apply
    if apply and not yes:
        if not confirm_yes_default_no(
            f"Delete matching GHCR package versions for {', '.join(plan.packages)}? [y/N] "
        ):
            print("Cancelled.", file=sys.stderr)
            return 1

    try:
        return run_cleanup(plan, dry_run=dry_run, token=token)
    except RuntimeError as exc:
        print(f"clean-images: {exc}", file=sys.stderr)
        return 1
