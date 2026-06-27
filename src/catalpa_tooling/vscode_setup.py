"""Scaffold VS Code tasks for Catalpa tooling repos."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from catalpa_tooling.config import ProjectConfig, load_project_config
from catalpa_tooling.repo_paths import repo_root_from_cwd
from catalpa_tooling.vscode_tasks import (
    MANAGED_MARKER_KEY,
    SETUP_VSCODE_GENERATOR_VERSION,
    WorkflowKind,
    build_extensions_json,
    build_settings_json,
    build_tasks_json,
)

VSCODE_DIR = ".vscode"
TASKS_FILE = "tasks.json"
EXTENSIONS_FILE = "extensions.json"
SETTINGS_FILE = "settings.json"

GITIGNORE_VSCODE_BLOCK = """# VSCode (setup-vscode)
.vscode/*
!.vscode/tasks.json
!.vscode/extensions.json
!.vscode/settings.json
"""

WorkflowOverride = Literal["auto", "docker"]


@dataclass(frozen=True)
class VscodeSetupStatus:
    repo_root: Path
    vscode_dir: Path
    tasks_present: bool
    tasks_managed: bool
    tasks_current: bool
    extensions_present: bool
    extensions_managed: bool
    extensions_current: bool
    settings_present: bool
    settings_managed: bool
    settings_current: bool
    gitignore_patched: bool
    workflow: WorkflowKind

    @property
    def ready(self) -> bool:
        return (
            self.tasks_present
            and self.tasks_managed
            and self.tasks_current
            and self.extensions_present
            and self.extensions_managed
            and self.extensions_current
            and self.settings_present
            and self.settings_managed
            and self.settings_current
            and self.gitignore_patched
        )


@dataclass(frozen=True)
class VscodeSetupPlan:
    repo_root: Path
    workflow: WorkflowKind
    write_tasks: bool
    write_extensions: bool
    write_settings: bool
    patch_gitignore: bool
    gitignore_path: Path
    gitignore_new_text: str | None
    tasks_path: Path
    tasks_content: str
    extensions_path: Path
    extensions_content: str
    settings_path: Path
    settings_content: str
    remove_tasks: bool
    remove_extensions: bool
    remove_settings: bool


def detect_workflow(
    config: ProjectConfig,
    *,
    override: WorkflowOverride = "auto",
) -> WorkflowKind:
    del override  # only docker (dk dev/full) tasks are generated
    if not (config.deploy_envs_dir / "dev" / "info.yaml").is_file():
        raise FileNotFoundError(
            f"Missing {config.deploy_envs_dir / 'dev' / 'info.yaml'} — "
            "setup-vscode requires a docker/envs/dev environment."
        )
    return WorkflowKind.DOCKER


def _include_full_stack(config: ProjectConfig) -> bool:
    return (config.deploy_envs_dir / "full" / "info.yaml").is_file()


def _json_dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, indent="\t") + "\n"


def _is_managed_json(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return data.get(MANAGED_MARKER_KEY) == SETUP_VSCODE_GENERATOR_VERSION


def _json_matches(path: Path, expected: dict[str, Any]) -> bool:
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return data == expected


def _gitignore_has_vscode_exceptions(text: str) -> bool:
    return "!.vscode/tasks.json" in text


def _plan_gitignore_patch(gitignore_path: Path) -> tuple[bool, str | None]:
    if not gitignore_path.is_file():
        new_text = GITIGNORE_VSCODE_BLOCK
        return True, new_text

    text = gitignore_path.read_text(encoding="utf-8")
    if _gitignore_has_vscode_exceptions(text):
        return False, None

    vscode_line = re.compile(r"^\.vscode/?\s*$", re.MULTILINE)
    if vscode_line.search(text):
        updated = vscode_line.sub(GITIGNORE_VSCODE_BLOCK.rstrip("\n"), text, count=1)
        if not updated.endswith("\n"):
            updated += "\n"
        return True, updated

    suffix = "" if text.endswith("\n") or not text else "\n"
    updated = text + suffix + "\n" + GITIGNORE_VSCODE_BLOCK
    return True, updated


def _build_file_contents(
    config: ProjectConfig,
    workflow: WorkflowKind,
) -> tuple[str, str, str]:
    tasks = build_tasks_json(
        workflow,
        include_full=_include_full_stack(config),
    )
    return (
        _json_dumps(tasks),
        _json_dumps(build_extensions_json()),
        _json_dumps(build_settings_json()),
    )


def inspect_status(
    *,
    repo_root: Path | None = None,
    workflow_override: WorkflowOverride = "auto",
) -> VscodeSetupStatus:
    root = repo_root or repo_root_from_cwd()
    config = load_project_config(root)
    workflow = detect_workflow(config, override=workflow_override)

    vscode_dir = root / VSCODE_DIR
    tasks_path = vscode_dir / TASKS_FILE
    extensions_path = vscode_dir / EXTENSIONS_FILE
    settings_path = vscode_dir / SETTINGS_FILE
    gitignore_path = root / ".gitignore"

    tasks_content, extensions_content, settings_content = _build_file_contents(
        config, workflow
    )
    expected_tasks = json.loads(tasks_content)
    expected_extensions = json.loads(extensions_content)
    expected_settings = json.loads(settings_content)

    patch_gitignore, _ = _plan_gitignore_patch(gitignore_path)

    return VscodeSetupStatus(
        repo_root=root,
        vscode_dir=vscode_dir,
        tasks_present=tasks_path.is_file(),
        tasks_managed=_is_managed_json(tasks_path),
        tasks_current=_json_matches(tasks_path, expected_tasks),
        extensions_present=extensions_path.is_file(),
        extensions_managed=_is_managed_json(extensions_path),
        extensions_current=_json_matches(extensions_path, expected_extensions),
        settings_present=settings_path.is_file(),
        settings_managed=_is_managed_json(settings_path),
        settings_current=_json_matches(settings_path, expected_settings),
        gitignore_patched=not patch_gitignore,
        workflow=workflow,
    )


def plan_setup(
    *,
    repo_root: Path | None = None,
    workflow_override: WorkflowOverride = "auto",
    force: bool = False,
) -> VscodeSetupPlan:
    root = repo_root or repo_root_from_cwd()
    config = load_project_config(root)
    workflow = detect_workflow(config, override=workflow_override)

    vscode_dir = root / VSCODE_DIR
    tasks_path = vscode_dir / TASKS_FILE
    extensions_path = vscode_dir / EXTENSIONS_FILE
    settings_path = vscode_dir / SETTINGS_FILE
    gitignore_path = root / ".gitignore"

    tasks_content, extensions_content, settings_content = _build_file_contents(
        config, workflow
    )
    expected_tasks = json.loads(tasks_content)
    expected_extensions = json.loads(extensions_content)
    expected_settings = json.loads(settings_content)

    def should_write(path: Path, expected: dict[str, Any]) -> bool:
        if force:
            return True
        if not path.is_file():
            return True
        if not _is_managed_json(path):
            return False
        return not _json_matches(path, expected)

    patch_gitignore, gitignore_new_text = _plan_gitignore_patch(gitignore_path)

    return VscodeSetupPlan(
        repo_root=root,
        workflow=workflow,
        write_tasks=should_write(tasks_path, expected_tasks),
        write_extensions=should_write(extensions_path, expected_extensions),
        write_settings=should_write(settings_path, expected_settings),
        patch_gitignore=patch_gitignore,
        gitignore_path=gitignore_path,
        gitignore_new_text=gitignore_new_text,
        tasks_path=tasks_path,
        tasks_content=tasks_content,
        extensions_path=extensions_path,
        extensions_content=extensions_content,
        settings_path=settings_path,
        settings_content=settings_content,
        remove_tasks=False,
        remove_extensions=False,
        remove_settings=False,
    )


def plan_remove(*, repo_root: Path | None = None) -> VscodeSetupPlan:
    root = repo_root or repo_root_from_cwd()
    config = load_project_config(root)
    workflow = detect_workflow(config)

    vscode_dir = root / VSCODE_DIR
    tasks_path = vscode_dir / TASKS_FILE
    extensions_path = vscode_dir / EXTENSIONS_FILE
    settings_path = vscode_dir / SETTINGS_FILE

    return VscodeSetupPlan(
        repo_root=root,
        workflow=workflow,
        write_tasks=False,
        write_extensions=False,
        write_settings=False,
        patch_gitignore=False,
        gitignore_path=root / ".gitignore",
        gitignore_new_text=None,
        tasks_path=tasks_path,
        tasks_content="",
        extensions_path=extensions_path,
        extensions_content="",
        settings_path=settings_path,
        settings_content="",
        remove_tasks=_is_managed_json(tasks_path),
        remove_extensions=_is_managed_json(extensions_path),
        remove_settings=_is_managed_json(settings_path),
    )


def apply_setup(plan: VscodeSetupPlan, *, dry_run: bool = False) -> None:
    if plan.write_tasks:
        if dry_run:
            print(f"Would write {plan.tasks_path}")
        else:
            plan.tasks_path.parent.mkdir(parents=True, exist_ok=True)
            plan.tasks_path.write_text(plan.tasks_content, encoding="utf-8")

    if plan.write_extensions:
        if dry_run:
            print(f"Would write {plan.extensions_path}")
        else:
            plan.extensions_path.parent.mkdir(parents=True, exist_ok=True)
            plan.extensions_path.write_text(plan.extensions_content, encoding="utf-8")

    if plan.write_settings:
        if dry_run:
            print(f"Would write {plan.settings_path}")
        else:
            plan.settings_path.parent.mkdir(parents=True, exist_ok=True)
            plan.settings_path.write_text(plan.settings_content, encoding="utf-8")

    if plan.patch_gitignore and plan.gitignore_new_text is not None:
        if dry_run:
            print(f"Would patch {plan.gitignore_path}")
        else:
            plan.gitignore_path.write_text(plan.gitignore_new_text, encoding="utf-8")


def apply_remove(plan: VscodeSetupPlan, *, dry_run: bool = False) -> None:
    for path, remove in (
        (plan.tasks_path, plan.remove_tasks),
        (plan.extensions_path, plan.remove_extensions),
        (plan.settings_path, plan.remove_settings),
    ):
        if not remove:
            continue
        if dry_run:
            print(f"Would remove {path}")
        else:
            path.unlink(missing_ok=True)

    if not dry_run:
        vscode_dir = plan.repo_root / VSCODE_DIR
        if vscode_dir.is_dir() and not any(vscode_dir.iterdir()):
            vscode_dir.rmdir()
