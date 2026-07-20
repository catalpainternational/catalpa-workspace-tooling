"""``dk cut-release`` — cut final releases, open next ``dev-*`` lines, or stage betas."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

import yaml

from catalpa_tooling.cli_confirm import confirm_yes_default_no
from catalpa_tooling.cut_release_version import (
    BumpKind,
    Version,
    format_beta_tag,
    format_dev_branch,
    format_v_tag,
    next_beta_w,
    parse_dev_branch,
    parse_v_tag,
)
from catalpa_tooling.run_cmd import format_shell_command

CutMode = Literal["release", "next-branch", "beta"]


@dataclass(frozen=True, slots=True)
class CutReleasePlan:
    mode: CutMode
    repo: Path
    version: Version
    release_tag: str | None
    next_branch: str | None
    from_branch: str | None
    beta_tag: str | None
    pin_submodules: tuple[tuple[str, str], ...]
    image_env: str | None
    image_tag_value: str | None
    set_default: bool
    update_gitmodules_branch: bool


class CutReleaseError(Exception):
    """User-facing failure for cut-release."""


def _git_q(
    repo: Path,
    args: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Quiet git for inspection (no command echo)."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _git(
    repo: Path,
    args: Sequence[str],
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(repo), *args]
    print(f"$ {format_shell_command(cmd)}", flush=True)
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def _git_out(repo: Path, args: Sequence[str]) -> str:
    return _git_q(repo, args).stdout.strip()


def _git_ok(repo: Path, args: Sequence[str]) -> bool:
    return _git_q(repo, args, check=False).returncode == 0


def resolve_git_repo(repo_root: Path, submodule: str | None) -> Path:
    """Return the git work tree to operate on (repo root or submodule)."""
    root = repo_root.resolve()
    if not submodule:
        if not _git_ok(root, ["rev-parse", "--git-dir"]):
            raise CutReleaseError(f"not a git repository: {root}")
        return root
    path = (root / submodule).resolve()
    if not path.is_dir():
        raise CutReleaseError(f"submodule path not found: {path}")
    if not _git_ok(path, ["rev-parse", "--git-dir"]):
        raise CutReleaseError(f"not a git repository (submodule): {path}")
    return path


def _working_tree_clean(repo: Path) -> bool:
    return _git_out(repo, ["status", "--porcelain"]) == ""


def _current_branch(repo: Path) -> str | None:
    name = _git_out(repo, ["branch", "--show-current"])
    return name or None


def _exact_tag_at_head(repo: Path) -> str | None:
    """Return a single exact tag pointing at HEAD, preferring ``vX.Y[.Z]``."""
    out = _git_q(repo, ["tag", "--points-at", "HEAD"], check=False).stdout.strip()
    if not out:
        return None
    tags = [t for t in out.splitlines() if t.strip()]
    finals = [t for t in tags if parse_v_tag(t)]
    if len(finals) == 1:
        return finals[0]
    if len(finals) > 1:
        finals.sort(key=lambda t: (-len(t), t))
        return finals[0]
    return tags[0] if len(tags) == 1 else None


def _list_tags(repo: Path) -> list[str]:
    out = _git_q(repo, ["tag", "-l"], check=False).stdout.strip()
    if not out:
        return []
    return [t.strip() for t in out.splitlines() if t.strip()]


def _remote_ref_exists(repo: Path, ref: str) -> bool:
    return _git_q(repo, ["ls-remote", "--exit-code", "origin", ref], check=False).returncode == 0


def _parse_pin_submodules(raw: Sequence[str] | None) -> tuple[tuple[str, str], ...]:
    if not raw:
        return ()
    out: list[tuple[str, str]] = []
    for item in raw:
        if "=" not in item:
            raise CutReleaseError(f"--pin-submodule expects PATH=REF, got {item!r}")
        path, ref = item.split("=", 1)
        path, ref = path.strip(), ref.strip()
        if not path or not ref:
            raise CutReleaseError(f"--pin-submodule expects PATH=REF, got {item!r}")
        out.append((path, ref))
    return tuple(out)


def build_cut_release_plan(
    *,
    repo: Path,
    bump: BumpKind | None,
    beta: bool,
    beta_w: int | None,
    tag_override: str | None,
    next_branch_override: str | None,
    set_default: bool,
    pin_submodules: Sequence[str] | None,
    image_env: str | None,
    allow_prod_beta: bool,
) -> CutReleasePlan:
    pins = _parse_pin_submodules(pin_submodules)

    if beta and bump is not None:
        raise CutReleaseError("--beta cannot be combined with --bump")
    if beta and set_default:
        raise CutReleaseError("--beta cannot be combined with --set-default")
    if beta and next_branch_override:
        raise CutReleaseError("--beta cannot be combined with --next-branch")

    branch = _current_branch(repo)
    head_tag = _exact_tag_at_head(repo)

    if beta:
        if not branch:
            raise CutReleaseError("Mode C (--beta) requires a checked-out branch (dev-X.Y[.Z])")
        version = parse_dev_branch(branch)
        if version is None:
            raise CutReleaseError(
                f"Mode C (--beta) requires branch matching dev-X.Y[.Z], got {branch!r}"
            )
        if tag_override:
            if not tag_override.startswith(format_v_tag(version) + ".beta."):
                raise CutReleaseError(
                    f"--tag override for --beta must be {format_v_tag(version)}.beta.W, "
                    f"got {tag_override!r}"
                )
            beta_tag = tag_override
        else:
            w = beta_w if beta_w is not None else next_beta_w(_list_tags(repo), version)
            beta_tag = format_beta_tag(version, w)
        if image_env == "prod" and not allow_prod_beta:
            raise CutReleaseError(
                "refusing --image-env prod with --beta (pass --allow-prod-beta to override)"
            )
        return CutReleasePlan(
            mode="beta",
            repo=repo,
            version=version,
            release_tag=None,
            next_branch=None,
            from_branch=branch,
            beta_tag=beta_tag,
            pin_submodules=pins,
            image_env=image_env,
            image_tag_value=beta_tag if image_env else None,
            set_default=False,
            update_gitmodules_branch=False,
        )

    # Mode B: on a final v* tag
    if branch is None and head_tag and parse_v_tag(head_tag):
        version = parse_v_tag(head_tag)
        assert version is not None
        if next_branch_override:
            next_branch = next_branch_override
        elif bump is not None:
            next_branch = format_dev_branch(version.bump(bump))
        else:
            raise CutReleaseError("Mode B (on v* tag) requires --bump or --next-branch")
        return CutReleasePlan(
            mode="next-branch",
            repo=repo,
            version=version,
            release_tag=None,
            next_branch=next_branch,
            from_branch=None,
            beta_tag=None,
            pin_submodules=pins,
            image_env=image_env,
            image_tag_value=None,
            set_default=set_default,
            update_gitmodules_branch=False,
        )

    # Mode A: on dev-* branch
    if branch and parse_dev_branch(branch):
        version = parse_dev_branch(branch)
        assert version is not None
        release_tag = tag_override or format_v_tag(version)
        if bump is not None:
            next_branch = next_branch_override or format_dev_branch(version.bump(bump))
        elif next_branch_override:
            next_branch = next_branch_override
        else:
            raise CutReleaseError(
                "Mode A (on dev-* branch) requires --bump or --next-branch"
            )
        return CutReleasePlan(
            mode="release",
            repo=repo,
            version=version,
            release_tag=release_tag,
            next_branch=next_branch,
            from_branch=branch,
            beta_tag=None,
            pin_submodules=pins,
            image_env=image_env,
            image_tag_value=release_tag if image_env else None,
            set_default=set_default,
            update_gitmodules_branch=True,
        )

    raise CutReleaseError(
        "HEAD must be a dev-X.Y[.Z] branch (Mode A/C) or a vX.Y[.Z] tag (Mode B); "
        f"got branch={branch!r} tag={head_tag!r}"
    )


def _print_plan(plan: CutReleasePlan, *, execute: bool) -> None:
    print("dk cut-release plan", file=sys.stderr)
    print(f"  mode:          {plan.mode}", file=sys.stderr)
    print(f"  repo:          {plan.repo}", file=sys.stderr)
    print(f"  version:       {format_omit_zeros_display(plan.version)}", file=sys.stderr)
    if plan.from_branch:
        print(f"  from_branch:   {plan.from_branch}", file=sys.stderr)
    if plan.release_tag:
        print(f"  release_tag:   {plan.release_tag}", file=sys.stderr)
    if plan.beta_tag:
        print(f"  beta_tag:      {plan.beta_tag}", file=sys.stderr)
    if plan.next_branch:
        print(f"  next_branch:   {plan.next_branch}", file=sys.stderr)
    print(f"  set_default:   {plan.set_default}", file=sys.stderr)
    if plan.pin_submodules:
        for path, ref in plan.pin_submodules:
            print(f"  pin_submodule: {path}={ref}", file=sys.stderr)
    if plan.image_env:
        print(f"  image_env:     {plan.image_env} → {plan.image_tag_value}", file=sys.stderr)
    print(
        f"  execute:       {execute} ({'mutations enabled' if execute else 'dry-run only'})",
        file=sys.stderr,
    )


def format_omit_zeros_display(version: Version) -> str:
    from catalpa_tooling.cut_release_version import format_omit_zeros

    return format_omit_zeros(version)


def _set_info_image_tag(info_path: Path, tag: str) -> None:
    text = info_path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise CutReleaseError(f"unexpected info.yaml structure: {info_path}")
    data["image_tag"] = tag
    info_path.write_text(
        yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _update_gitmodules_branch(repo: Path, old_branch: str, new_branch: str) -> bool:
    gm = repo / ".gitmodules"
    if not gm.is_file():
        return False
    text = gm.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^(\s*branch\s*=\s*){re.escape(old_branch)}\s*$",
        re.MULTILINE,
    )
    new_text, n = pattern.subn(rf"\g<1>{new_branch}", text)
    if n == 0:
        return False
    gm.write_text(new_text, encoding="utf-8")
    return True


def _pin_submodule(parent: Path, path: str, ref: str) -> None:
    sub = parent / path
    if not sub.is_dir():
        raise CutReleaseError(f"submodule path not found: {sub}")
    _git(sub, ["fetch", "--tags", "--prune", "origin"])
    # Prefer exact tag checkout when ref looks like a tag
    if not _git_ok(sub, ["checkout", "--detach", ref]):
        raise CutReleaseError(f"failed to checkout {ref!r} in {path}")
    _git(parent, ["add", path])


def _commit_if_staged(repo: Path, message: str) -> bool:
    staged = _git_out(repo, ["diff", "--cached", "--name-only"])
    if not staged:
        return False
    _git(repo, ["commit", "-m", message])
    return True


def _gh_set_default_branch(repo: Path, branch: str) -> None:
    # Resolve owner/name from origin
    url = _git_out(repo, ["remote", "get-url", "origin"])
    # git@github.com:org/repo.git or https://github.com/org/repo.git
    m = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<name>[^/.]+)", url)
    if not m:
        raise CutReleaseError(f"cannot parse GitHub repo from origin URL: {url}")
    slug = f"{m.group('owner')}/{m.group('name')}"
    cmd = ["gh", "repo", "edit", slug, "--default-branch", branch]
    print(f"$ {format_shell_command(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def execute_cut_release_plan(
    plan: CutReleasePlan,
    *,
    execute: bool,
    yes: bool,
    allow_dirty: bool = False,
) -> int:
    _print_plan(plan, execute=execute)
    if not execute:
        print(
            "\nDry-run only. Re-run with --execute to perform mutations.",
            file=sys.stderr,
        )
        return 0

    if not yes:
        if not confirm_yes_default_no("Proceed with cut-release mutations? [y/N]: "):
            print("Cancelled.", file=sys.stderr)
            return 1

    repo = plan.repo
    if not _working_tree_clean(repo):
        if not allow_dirty:
            raise CutReleaseError(
                f"working tree is not clean: {repo} "
                "(commit/stash changes, or pass --allow-dirty)"
            )
        dirty = _git_out(repo, ["status", "--porcelain"])
        print(
            "WARNING: proceeding with a dirty working tree (--allow-dirty):\n"
            f"{dirty}",
            file=sys.stderr,
        )

    _git(repo, ["fetch", "--tags", "--prune", "origin"])

    # Pins first (on current branch)
    if plan.pin_submodules:
        for path, ref in plan.pin_submodules:
            _pin_submodule(repo, path, ref)
        _commit_if_staged(
            repo,
            "pin " + ", ".join(f"{p}={r}" for p, r in plan.pin_submodules),
        )

    if plan.mode == "beta":
        assert plan.beta_tag and plan.from_branch
        if _remote_ref_exists(repo, f"refs/tags/{plan.beta_tag}") or _git_ok(
            repo, ["rev-parse", "-q", "--verify", f"refs/tags/{plan.beta_tag}"]
        ):
            raise CutReleaseError(f"tag already exists: {plan.beta_tag}")
        if plan.image_env and plan.image_tag_value:
            info = repo / "docker" / "envs" / plan.image_env / "info.yaml"
            if not info.is_file():
                raise CutReleaseError(f"info.yaml not found: {info}")
            _set_info_image_tag(info, plan.image_tag_value)
            _git(repo, ["add", str(info.relative_to(repo))])
            _commit_if_staged(repo, f"staging image_tag {plan.image_tag_value}")
        _git(repo, ["tag", "-a", plan.beta_tag, "-m", f"Beta {plan.beta_tag}"])
        _git(repo, ["push", "origin", plan.from_branch])
        _git(repo, ["push", "origin", plan.beta_tag])
        print(
            f"\nBeta {plan.beta_tag} pushed. Wait for CI images, then deploy staging manually.",
            file=sys.stderr,
        )
        return 0

    if plan.mode == "next-branch":
        assert plan.next_branch
        if _remote_ref_exists(repo, f"refs/heads/{plan.next_branch}"):
            raise CutReleaseError(f"remote branch already exists: {plan.next_branch}")
        _git(repo, ["branch", plan.next_branch, "HEAD"])
        _git(repo, ["push", "-u", "origin", plan.next_branch])
        if plan.set_default:
            _gh_set_default_branch(repo, plan.next_branch)
        print(f"\nCreated {plan.next_branch} from current tag tip.", file=sys.stderr)
        return 0

    # Mode A — release
    assert plan.release_tag and plan.next_branch and plan.from_branch
    if _remote_ref_exists(repo, f"refs/tags/{plan.release_tag}") or _git_ok(
        repo, ["rev-parse", "-q", "--verify", f"refs/tags/{plan.release_tag}"]
    ):
        raise CutReleaseError(f"tag already exists: {plan.release_tag}")
    if _remote_ref_exists(repo, f"refs/heads/{plan.next_branch}"):
        raise CutReleaseError(f"remote branch already exists: {plan.next_branch}")

    if plan.image_env and plan.image_tag_value:
        info = repo / "docker" / "envs" / plan.image_env / "info.yaml"
        if not info.is_file():
            raise CutReleaseError(f"info.yaml not found: {info}")
        _set_info_image_tag(info, plan.image_tag_value)
        _git(repo, ["add", str(info.relative_to(repo))])
        _commit_if_staged(repo, f"image_tag {plan.image_tag_value}")

    if plan.update_gitmodules_branch:
        if _update_gitmodules_branch(repo, plan.from_branch, plan.next_branch):
            _git(repo, ["add", ".gitmodules"])
            # Commit on from_branch before merge so main gets it; also needed on next branch.
            # We commit here on from_branch; after creating next_branch tip includes it via merge.
            _commit_if_staged(
                repo,
                f"gitmodules: track {plan.next_branch}",
            )

    # Merge to main
    _git(repo, ["checkout", "main"])
    _git(repo, ["pull", "--ff-only", "origin", "main"])
    # Prefer ff-only from the ready branch tip
    merge = _git(
        repo,
        ["merge", "--ff-only", plan.from_branch],
        check=False,
    )
    if merge.returncode != 0:
        # Fall back to merge commit with confirmation already given
        _git(repo, ["merge", "--no-ff", plan.from_branch, "-m", f"Merge {plan.from_branch}"])
    _git(repo, ["tag", "-a", plan.release_tag, "-m", f"Release {plan.release_tag}"])
    _git(repo, ["push", "origin", "main"])
    _git(repo, ["push", "origin", plan.release_tag])

    # Next branch from tag tip
    _git(repo, ["branch", plan.next_branch, plan.release_tag])
    # If gitmodules was updated on from_branch for next name, tip already has it via merge.
    # If update only made sense on next line, ensure .gitmodules on next_branch points to itself:
    _git(repo, ["checkout", plan.next_branch])
    if plan.update_gitmodules_branch:
        # Ensure tracking matches next_branch even if from_branch name differed in history
        if _update_gitmodules_branch(repo, plan.from_branch, plan.next_branch):
            _git(repo, ["add", ".gitmodules"])
            _commit_if_staged(repo, f"gitmodules: track {plan.next_branch}")
    _git(repo, ["push", "-u", "origin", plan.next_branch])
    # Also push updated from_branch if we committed image_tag/gitmodules there before merge
    _git(repo, ["push", "origin", plan.from_branch], check=False)

    if plan.set_default:
        _gh_set_default_branch(repo, plan.next_branch)

    print(
        f"\nRelease {plan.release_tag} on main; next line {plan.next_branch}. "
        "Wait for CI images before relying on image_tag deploys.",
        file=sys.stderr,
    )
    return 0


def cut_release(
    *,
    repo_root: Path,
    bump: BumpKind | None = None,
    beta: bool = False,
    beta_w: int | None = None,
    submodule: str | None = None,
    execute: bool = False,
    yes: bool = False,
    set_default: bool = False,
    tag: str | None = None,
    next_branch: str | None = None,
    pin_submodule: Sequence[str] | None = None,
    image_env: str | None = None,
    allow_prod_beta: bool = False,
    allow_dirty: bool = False,
) -> int:
    """Typed entrypoint for ``dk cut-release``."""
    try:
        repo = resolve_git_repo(repo_root, submodule)
        plan = build_cut_release_plan(
            repo=repo,
            bump=bump,
            beta=beta,
            beta_w=beta_w,
            tag_override=tag,
            next_branch_override=next_branch,
            set_default=set_default,
            pin_submodules=pin_submodule,
            image_env=image_env,
            allow_prod_beta=allow_prod_beta,
        )
        return execute_cut_release_plan(
            plan, execute=execute, yes=yes, allow_dirty=allow_dirty
        )
    except CutReleaseError as exc:
        print(f"dk cut-release: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"dk cut-release: command failed: {exc}", file=sys.stderr)
        return exc.returncode or 1
