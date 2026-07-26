"""``dk cut-release`` / ``dk next-branch`` — cut tags and open next ``dev-*`` lines."""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from catalpa_tooling.cli_confirm import confirm_yes_default_no
from catalpa_tooling.cut_release_version import (
    BumpKind,
    Version,
    format_beta_tag,
    format_dev_branch,
    format_omit_zeros,
    format_v_tag,
    next_beta_w,
    parse_beta_tag,
    parse_dev_branch,
    parse_v_tag,
)
from catalpa_tooling.run_cmd import format_shell_command

CutMode = Literal["final", "beta", "next-branch"]


@dataclass(frozen=True, slots=True)
class CutReleasePlan:
    mode: CutMode
    repo: Path
    version: Version
    release_tag: str | None
    next_branch: str | None
    from_branch: str | None
    beta_tag: str | None
    set_default: bool
    update_gitmodules_branch: bool
    # Prior line name for .gitmodules rewrite (next-branch only).
    prior_dev_branch: str | None = None


class CutReleaseError(Exception):
    """User-facing failure for cut-release / next-branch."""


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


def _require_named_branch(repo: Path, *, verb: str) -> str:
    branch = _current_branch(repo)
    if not branch:
        raise CutReleaseError(f"{verb} requires a checked-out named branch (not detached HEAD)")
    return branch


def _parse_next_branch_spec(spec: str, version: Version) -> str:
    """Resolve bump keyword or explicit ``dev-X.Y[.Z]`` to a branch name."""
    if spec in ("major", "minor", "hotfix"):
        return format_dev_branch(version.bump(spec))  # type: ignore[arg-type]
    if parse_dev_branch(spec) is not None:
        return spec
    raise CutReleaseError(
        f"next-branch expects major|minor|hotfix or a dev-X.Y[.Z] name, got {spec!r}"
    )


def build_final_plan(*, repo: Path) -> CutReleasePlan:
    branch = _require_named_branch(repo, verb="cut-release final")
    version = parse_dev_branch(branch)
    if version is None:
        raise CutReleaseError(
            f"cut-release final requires branch matching dev-X.Y[.Z], got {branch!r}"
        )
    return CutReleasePlan(
        mode="final",
        repo=repo,
        version=version,
        release_tag=format_v_tag(version),
        next_branch=None,
        from_branch=branch,
        beta_tag=None,
        set_default=False,
        update_gitmodules_branch=False,
    )


def build_beta_plan(
    *,
    repo: Path,
    beta_w: int | None,
    tag_override: str | None,
) -> CutReleasePlan:
    branch = _require_named_branch(repo, verb="cut-release beta")
    if tag_override is not None and beta_w is not None:
        raise CutReleaseError("cut-release beta: pass either positional W or --tag, not both")

    version = parse_dev_branch(branch)
    if version is not None:
        if tag_override:
            if not tag_override.startswith(format_v_tag(version) + ".beta."):
                raise CutReleaseError(
                    f"--tag for beta on {branch} must be {format_v_tag(version)}.beta.W, "
                    f"got {tag_override!r}"
                )
            if parse_beta_tag(tag_override) is None:
                raise CutReleaseError(f"--tag is not a valid beta tag: {tag_override!r}")
            beta_tag = tag_override
        else:
            w = beta_w if beta_w is not None else next_beta_w(_list_tags(repo), version)
            beta_tag = format_beta_tag(version, w)
        return CutReleasePlan(
            mode="beta",
            repo=repo,
            version=version,
            release_tag=None,
            next_branch=None,
            from_branch=branch,
            beta_tag=beta_tag,
            set_default=False,
            update_gitmodules_branch=False,
        )

    # Non-dev-* branch: --tag is required (no version to infer).
    if not tag_override:
        raise CutReleaseError(
            f"cut-release beta: branch {branch!r} is not dev-X.Y[.Z]; "
            "pass --tag vX.Y[.Z].beta.W to set the beta tag"
        )
    parsed = parse_beta_tag(tag_override)
    if parsed is None:
        raise CutReleaseError(
            f"--tag must be a beta tag vX.Y[.Z].beta.W, got {tag_override!r}"
        )
    version, _w = parsed
    return CutReleasePlan(
        mode="beta",
        repo=repo,
        version=version,
        release_tag=None,
        next_branch=None,
        from_branch=branch,
        beta_tag=tag_override,
        set_default=False,
        update_gitmodules_branch=False,
    )


def build_next_branch_plan(
    *,
    repo: Path,
    spec: str,
    set_default: bool,
) -> CutReleasePlan:
    head_tag = _exact_tag_at_head(repo)
    if not head_tag or parse_v_tag(head_tag) is None:
        raise CutReleaseError(
            "next-branch requires HEAD at a final vX.Y[.Z] tag tip "
            f"(detached or any branch); got branch={_current_branch(repo)!r} "
            f"tag={head_tag!r}"
        )
    version = parse_v_tag(head_tag)
    assert version is not None
    next_branch = _parse_next_branch_spec(spec, version)
    return CutReleasePlan(
        mode="next-branch",
        repo=repo,
        version=version,
        release_tag=None,
        next_branch=next_branch,
        from_branch=None,
        beta_tag=None,
        set_default=set_default,
        update_gitmodules_branch=True,
        prior_dev_branch=format_dev_branch(version),
    )


def _print_plan(plan: CutReleasePlan, *, execute: bool, command: str) -> None:
    print(f"dk {command} plan", file=sys.stderr)
    print(f"  mode:          {plan.mode}", file=sys.stderr)
    print(f"  repo:          {plan.repo}", file=sys.stderr)
    print(f"  version:       {format_omit_zeros(plan.version)}", file=sys.stderr)
    if plan.from_branch:
        print(f"  from_branch:   {plan.from_branch}", file=sys.stderr)
    if plan.release_tag:
        print(f"  release_tag:   {plan.release_tag}", file=sys.stderr)
    if plan.beta_tag:
        print(f"  beta_tag:      {plan.beta_tag}", file=sys.stderr)
    if plan.next_branch:
        print(f"  next_branch:   {plan.next_branch}", file=sys.stderr)
    if plan.mode == "next-branch":
        print(f"  set_default:   {plan.set_default}", file=sys.stderr)
    print(
        f"  execute:       {execute} ({'mutations enabled' if execute else 'dry-run only'})",
        file=sys.stderr,
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


def _commit_if_staged(repo: Path, message: str) -> bool:
    staged = _git_out(repo, ["diff", "--cached", "--name-only"])
    if not staged:
        return False
    _git(repo, ["commit", "-m", message])
    return True


def _gh_set_default_branch(repo: Path, branch: str) -> None:
    url = _git_out(repo, ["remote", "get-url", "origin"])
    m = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<name>[^/.]+)", url)
    if not m:
        raise CutReleaseError(f"cannot parse GitHub repo from origin URL: {url}")
    slug = f"{m.group('owner')}/{m.group('name')}"
    cmd = ["gh", "repo", "edit", slug, "--default-branch", branch]
    print(f"$ {format_shell_command(cmd)}", flush=True)
    subprocess.run(cmd, check=True)


def _print_final_suggestions(plan: CutReleasePlan) -> None:
    assert plan.release_tag is not None
    v = plan.version
    print(
        f"\nRelease {plan.release_tag} cut on main.\n"
        "\n"
        "Open the next line when ready (from this tag tip):\n"
        f"  uv run dk next-branch hotfix          # → {format_dev_branch(v.bump('hotfix'))}\n"
        f"  uv run dk next-branch minor           # → {format_dev_branch(v.bump('minor'))}\n"
        f"  uv run dk next-branch major           # → {format_dev_branch(v.bump('major'))}\n"
        "  uv run dk next-branch hotfix --set-default --execute\n"
        "\n"
        "Point an env at this tag by setting image_tag in docker/envs/<env>/info.yaml\n"
        "(then deploy yourself — cut-release never runs dk <env> up).",
        file=sys.stderr,
    )


def _print_beta_suggestions(plan: CutReleasePlan) -> None:
    assert plan.beta_tag is not None
    print(
        f"\nBeta {plan.beta_tag} pushed. Wait for CI images, then deploy staging manually.\n"
        "\n"
        "Point an env at this tag by setting image_tag in docker/envs/<env>/info.yaml\n"
        "(then deploy yourself — cut-release never runs dk <env> up).",
        file=sys.stderr,
    )


def _prepare_execute(
    plan: CutReleasePlan,
    *,
    execute: bool,
    yes: bool,
    allow_dirty: bool,
    command: str,
) -> int | None:
    """Print plan; return exit code if done (dry-run/cancel), else None to continue."""
    _print_plan(plan, execute=execute, command=command)
    if not execute:
        print(
            "\nDry-run only. Re-run with --execute to perform mutations.",
            file=sys.stderr,
        )
        return 0

    if not yes:
        if not confirm_yes_default_no(f"Proceed with {command} mutations? [y/N]: "):
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
    return None


def execute_final_plan(
    plan: CutReleasePlan,
    *,
    execute: bool,
    yes: bool,
    allow_dirty: bool = False,
) -> int:
    assert plan.mode == "final" and plan.release_tag and plan.from_branch
    early = _prepare_execute(
        plan, execute=execute, yes=yes, allow_dirty=allow_dirty, command="cut-release"
    )
    if early is not None:
        return early

    repo = plan.repo
    if _remote_ref_exists(repo, f"refs/tags/{plan.release_tag}") or _git_ok(
        repo, ["rev-parse", "-q", "--verify", f"refs/tags/{plan.release_tag}"]
    ):
        raise CutReleaseError(f"tag already exists: {plan.release_tag}")

    _git(repo, ["checkout", "main"])
    _git(repo, ["pull", "--ff-only", "origin", "main"])
    merge = _git(
        repo,
        ["merge", "--ff-only", plan.from_branch],
        check=False,
    )
    if merge.returncode != 0:
        _git(repo, ["merge", "--no-ff", plan.from_branch, "-m", f"Merge {plan.from_branch}"])
    _git(repo, ["tag", "-a", plan.release_tag, "-m", f"Release {plan.release_tag}"])
    _git(repo, ["push", "origin", "main"])
    _git(repo, ["push", "origin", plan.release_tag])
    _print_final_suggestions(plan)
    return 0


def execute_beta_plan(
    plan: CutReleasePlan,
    *,
    execute: bool,
    yes: bool,
    allow_dirty: bool = False,
) -> int:
    assert plan.mode == "beta" and plan.beta_tag and plan.from_branch
    early = _prepare_execute(
        plan, execute=execute, yes=yes, allow_dirty=allow_dirty, command="cut-release"
    )
    if early is not None:
        return early

    repo = plan.repo
    if _remote_ref_exists(repo, f"refs/tags/{plan.beta_tag}") or _git_ok(
        repo, ["rev-parse", "-q", "--verify", f"refs/tags/{plan.beta_tag}"]
    ):
        raise CutReleaseError(f"tag already exists: {plan.beta_tag}")
    _git(repo, ["tag", "-a", plan.beta_tag, "-m", f"Beta {plan.beta_tag}"])
    _git(repo, ["push", "origin", plan.from_branch])
    _git(repo, ["push", "origin", plan.beta_tag])
    _print_beta_suggestions(plan)
    return 0


def execute_next_branch_plan(
    plan: CutReleasePlan,
    *,
    execute: bool,
    yes: bool,
    allow_dirty: bool = False,
) -> int:
    assert plan.mode == "next-branch" and plan.next_branch
    early = _prepare_execute(
        plan, execute=execute, yes=yes, allow_dirty=allow_dirty, command="next-branch"
    )
    if early is not None:
        return early

    repo = plan.repo
    if _remote_ref_exists(repo, f"refs/heads/{plan.next_branch}"):
        raise CutReleaseError(f"remote branch already exists: {plan.next_branch}")

    _git(repo, ["branch", plan.next_branch, "HEAD"])
    _git(repo, ["checkout", plan.next_branch])
    if plan.update_gitmodules_branch and plan.prior_dev_branch:
        if _update_gitmodules_branch(repo, plan.prior_dev_branch, plan.next_branch):
            _git(repo, ["add", ".gitmodules"])
            _commit_if_staged(repo, f"gitmodules: track {plan.next_branch}")
    _git(repo, ["push", "-u", "origin", plan.next_branch])
    if plan.set_default:
        _gh_set_default_branch(repo, plan.next_branch)
    print(f"\nCreated {plan.next_branch} from current tag tip.", file=sys.stderr)
    return 0


def _run(
    *,
    repo_root: Path,
    submodule: str | None,
    label: str,
    build,
    execute_fn,
    execute: bool,
    yes: bool,
    allow_dirty: bool,
) -> int:
    try:
        repo = resolve_git_repo(repo_root, submodule)
        plan = build(repo=repo)
        return execute_fn(plan, execute=execute, yes=yes, allow_dirty=allow_dirty)
    except CutReleaseError as exc:
        print(f"dk {label}: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"dk {label}: command failed: {exc}", file=sys.stderr)
        return exc.returncode or 1


def cut_release_final(
    *,
    repo_root: Path,
    submodule: str | None = None,
    execute: bool = False,
    yes: bool = False,
    allow_dirty: bool = False,
) -> int:
    """Typed entrypoint for ``dk cut-release final``."""
    return _run(
        repo_root=repo_root,
        submodule=submodule,
        label="cut-release",
        build=build_final_plan,
        execute_fn=execute_final_plan,
        execute=execute,
        yes=yes,
        allow_dirty=allow_dirty,
    )


def cut_release_beta(
    *,
    repo_root: Path,
    beta_w: int | None = None,
    tag: str | None = None,
    submodule: str | None = None,
    execute: bool = False,
    yes: bool = False,
    allow_dirty: bool = False,
) -> int:
    """Typed entrypoint for ``dk cut-release beta``."""

    def build(*, repo: Path) -> CutReleasePlan:
        return build_beta_plan(repo=repo, beta_w=beta_w, tag_override=tag)

    return _run(
        repo_root=repo_root,
        submodule=submodule,
        label="cut-release",
        build=build,
        execute_fn=execute_beta_plan,
        execute=execute,
        yes=yes,
        allow_dirty=allow_dirty,
    )


def next_branch(
    *,
    repo_root: Path,
    spec: str,
    set_default: bool = False,
    submodule: str | None = None,
    execute: bool = False,
    yes: bool = False,
    allow_dirty: bool = False,
) -> int:
    """Typed entrypoint for ``dk next-branch``."""

    def build(*, repo: Path) -> CutReleasePlan:
        return build_next_branch_plan(repo=repo, spec=spec, set_default=set_default)

    return _run(
        repo_root=repo_root,
        submodule=submodule,
        label="next-branch",
        build=build,
        execute_fn=execute_next_branch_plan,
        execute=execute,
        yes=yes,
        allow_dirty=allow_dirty,
    )
