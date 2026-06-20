"""One-time zsh + direnv bootstrap for Catalpa tooling repos."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from catalpa_tooling.shell_assets import catalpa_direnv_zsh_path

MARKER_START = "# >>> catalpa-shell-setup >>>"
MARKER_END = "# <<< catalpa-shell-setup <<<"
DIRENV_HOOK_LINE = 'eval "$(direnv hook zsh)"'
CATALPA_SOURCE_LINE = (
    '[[ -f "${XDG_CONFIG_HOME:-$HOME/.config}/catalpa/direnv.zsh" ]] && \\\n'
    '  source "${XDG_CONFIG_HOME:-$HOME/.config}/catalpa/direnv.zsh"'
)
DEFAULT_CATALPA_CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "catalpa"
DEFAULT_CATALPA_DIRENV_DEST = DEFAULT_CATALPA_CONFIG_DIR / "direnv.zsh"


@dataclass(frozen=True)
class ShellSetupStatus:
    zshrc_path: Path
    zshrc_exists: bool
    catalpa_block_present: bool
    direnv_hook_present: bool
    catalpa_source_present: bool
    catalpa_direnv_installed: bool
    catalpa_direnv_matches_package: bool
    legacy_dk_wrapper: bool
    legacy_argcomplete_eval: bool


@dataclass(frozen=True)
class ShellSetupPlan:
    zshrc_path: Path
    catalpa_direnv_dest: Path
    write_catalpa_direnv: bool
    patch_zshrc: bool
    zshrc_block: str
    warnings: tuple[str, ...]


def default_zshrc_path() -> Path:
    zdot = os.environ.get("ZDOTDIR")
    if zdot:
        return Path(zdot).expanduser() / ".zshrc"
    return Path.home() / ".zshrc"


def build_zshrc_block(*, include_direnv_hook: bool, include_completion: bool) -> str:
    lines = [MARKER_START]
    if include_direnv_hook:
        lines.append(DIRENV_HOOK_LINE)
    if include_completion:
        lines.append(CATALPA_SOURCE_LINE)
    lines.append(MARKER_END)
    return "\n".join(lines) + "\n"


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _legacy_warnings(zshrc_text: str) -> tuple[str, ...]:
    warnings: list[str] = []
    if re.search(r"\bdk\s*\(\s*\)\s*\{[^}]*uv run dk", zshrc_text):
        warnings.append(
            "Found legacy dk() { uv run dk … } wrapper in ~/.zshrc — remove it; "
            "direnv PATH_add replaces it."
        )
    if "register-python-argcomplete" in zshrc_text and MARKER_START not in zshrc_text:
        warnings.append(
            "Found global register-python-argcomplete in ~/.zshrc — remove it; "
            "catalpa-direnv.zsh registers completion per repo."
        )
    return tuple(warnings)


def inspect_status(
    *,
    zshrc_path: Path | None = None,
    catalpa_direnv_dest: Path | None = None,
) -> ShellSetupStatus:
    zshrc = zshrc_path or default_zshrc_path()
    dest = catalpa_direnv_dest or DEFAULT_CATALPA_DIRENV_DEST
    zshrc_text = _read_text(zshrc)
    package_text = catalpa_direnv_zsh_path().read_text(encoding="utf-8")
    dest_text = _read_text(dest)
    block = extract_catalpa_block(zshrc_text)
    legacy = _legacy_warnings(zshrc_text)
    return ShellSetupStatus(
        zshrc_path=zshrc,
        zshrc_exists=zshrc.is_file(),
        catalpa_block_present=block is not None,
        direnv_hook_present=DIRENV_HOOK_LINE in zshrc_text,
        catalpa_source_present="catalpa/direnv.zsh" in zshrc_text,
        catalpa_direnv_installed=dest.is_file(),
        catalpa_direnv_matches_package=dest_text == package_text,
        legacy_dk_wrapper=any("dk() {" in w for w in legacy),
        legacy_argcomplete_eval=any("register-python-argcomplete" in w for w in legacy),
    )


def extract_catalpa_block(zshrc_text: str) -> str | None:
    start = zshrc_text.find(MARKER_START)
    if start == -1:
        return None
    end = zshrc_text.find(MARKER_END, start)
    if end == -1:
        return None
    end += len(MARKER_END)
    trailing = zshrc_text[end : end + 1]
    if trailing == "\n":
        end += 1
    return zshrc_text[start:end]


def _normalize_block(block: str) -> str:
    return block.rstrip("\n")


def patch_zshrc_content(
    zshrc_text: str,
    block: str,
) -> str:
    normalized = _normalize_block(block)
    existing = extract_catalpa_block(zshrc_text)
    if existing is not None:
        if _normalize_block(existing) == normalized:
            return zshrc_text
        return zshrc_text.replace(existing, normalized, 1)
    if zshrc_text and not zshrc_text.endswith("\n"):
        zshrc_text += "\n"
    if zshrc_text and not zshrc_text.endswith("\n\n"):
        zshrc_text += "\n"
    return zshrc_text + normalized + "\n"


def plan_setup(
    *,
    zshrc_path: Path | None = None,
    catalpa_direnv_dest: Path | None = None,
    skip_direnv_hook: bool = False,
    skip_completion: bool = False,
) -> ShellSetupPlan:
    zshrc = zshrc_path or default_zshrc_path()
    dest = catalpa_direnv_dest or DEFAULT_CATALPA_DIRENV_DEST
    zshrc_text = _read_text(zshrc)
    package_text = catalpa_direnv_zsh_path().read_text(encoding="utf-8")
    dest_text = _read_text(dest)
    block = build_zshrc_block(
        include_direnv_hook=not skip_direnv_hook,
        include_completion=not skip_completion,
    )
    new_zshrc = patch_zshrc_content(zshrc_text, block)
    return ShellSetupPlan(
        zshrc_path=zshrc,
        catalpa_direnv_dest=dest,
        write_catalpa_direnv=not skip_completion and dest_text != package_text,
        patch_zshrc=new_zshrc != zshrc_text,
        zshrc_block=block,
        warnings=_legacy_warnings(zshrc_text),
    )


def apply_setup(
    plan: ShellSetupPlan,
    *,
    dry_run: bool = False,
) -> None:
    if plan.write_catalpa_direnv:
        if dry_run:
            print(f"Would write {plan.catalpa_direnv_dest}")
        else:
            plan.catalpa_direnv_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(catalpa_direnv_zsh_path(), plan.catalpa_direnv_dest)

    if plan.patch_zshrc:
        if dry_run:
            print(f"Would patch {plan.zshrc_path}")
        else:
            zshrc_text = _read_text(plan.zshrc_path)
            plan.zshrc_path.parent.mkdir(parents=True, exist_ok=True)
            plan.zshrc_path.write_text(
                patch_zshrc_content(zshrc_text, plan.zshrc_block),
                encoding="utf-8",
            )


@dataclass(frozen=True)
class ShellRemovePlan:
    zshrc_path: Path
    catalpa_direnv_dest: Path
    remove_zshrc_block: bool
    remove_catalpa_direnv: bool


def remove_catalpa_block(zshrc_text: str) -> tuple[str, bool]:
    existing = extract_catalpa_block(zshrc_text)
    if existing is None:
        return zshrc_text, False
    result = zshrc_text.replace(existing, "", 1)
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    result = result.rstrip("\n")
    if result:
        result += "\n"
    return result, True


def plan_remove(
    *,
    zshrc_path: Path | None = None,
    catalpa_direnv_dest: Path | None = None,
) -> ShellRemovePlan:
    zshrc = zshrc_path or default_zshrc_path()
    dest = catalpa_direnv_dest or DEFAULT_CATALPA_DIRENV_DEST
    zshrc_text = _read_text(zshrc)
    _, remove_block = remove_catalpa_block(zshrc_text)
    return ShellRemovePlan(
        zshrc_path=zshrc,
        catalpa_direnv_dest=dest,
        remove_zshrc_block=remove_block,
        remove_catalpa_direnv=dest.is_file(),
    )


def apply_remove(
    plan: ShellRemovePlan,
    *,
    dry_run: bool = False,
) -> None:
    if plan.remove_zshrc_block:
        if dry_run:
            print(f"Would remove catalpa block from {plan.zshrc_path}")
        else:
            zshrc_text = _read_text(plan.zshrc_path)
            updated, _ = remove_catalpa_block(zshrc_text)
            plan.zshrc_path.write_text(updated, encoding="utf-8")

    if plan.remove_catalpa_direnv:
        if dry_run:
            print(f"Would remove {plan.catalpa_direnv_dest}")
        else:
            plan.catalpa_direnv_dest.unlink()
            parent = plan.catalpa_direnv_dest.parent
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()


@dataclass(frozen=True)
class NextStepsContext:
    cwd: Path
    zshrc_path: Path
    zshrc_changed: bool
    in_tooling_repo: bool
    has_envrc: bool
    direnv_loaded: bool
    dk_on_path: bool


def tooling_repo_at(path: Path) -> bool:
    return (path / "tooling.yaml").is_file()


def next_steps_context(
    *,
    zshrc_path: Path | None = None,
    zshrc_changed: bool = False,
    cwd: Path | None = None,
    environ: dict[str, str] | None = None,
) -> NextStepsContext:
    repo_root = (cwd or Path.cwd()).resolve()
    env = environ if environ is not None else os.environ
    return NextStepsContext(
        cwd=repo_root,
        zshrc_path=zshrc_path or default_zshrc_path(),
        zshrc_changed=zshrc_changed,
        in_tooling_repo=tooling_repo_at(repo_root),
        has_envrc=(repo_root / ".envrc").is_file(),
        direnv_loaded=bool(env.get("DIRENV_DIR")),
        dk_on_path=shutil.which("dk") is not None,
    )


def build_next_steps(ctx: NextStepsContext) -> tuple[str, ...]:
    """Return shell commands for the user (cannot be run in the parent shell automatically)."""
    steps: list[str] = []

    needs_zshrc_reload = ctx.zshrc_changed or (
        ctx.in_tooling_repo and ctx.has_envrc and not ctx.direnv_loaded
    )

    if needs_zshrc_reload:
        cmd = f"source {ctx.zshrc_path}"
        if ctx.in_tooling_repo and ctx.has_envrc:
            cmd += " && direnv reload"
        steps.append(cmd)

    if not ctx.in_tooling_repo:
        steps.append("cd into your tooling repo   # direnv loads .envrc on entry")
        if needs_zshrc_reload:
            steps.append("direnv allow   # once per repo, after cd")

    if (
        ctx.in_tooling_repo
        and ctx.has_envrc
        and not ctx.direnv_loaded
        and not needs_zshrc_reload
    ):
        steps.append("direnv reload")

    if not ctx.dk_on_path:
        steps.append("whence dk   # → …/.venv/bin/dk")

    return tuple(steps)
