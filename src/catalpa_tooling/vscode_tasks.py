"""Build VS Code ``tasks.json`` content for Catalpa tooling repos."""

from __future__ import annotations

from enum import Enum
from typing import Any

SETUP_VSCODE_GENERATOR_VERSION = "4"
MANAGED_MARKER_KEY = "_catalpa_setup_vscode"

PATH_ENV = (
    "${env:HOME}/.local/bin:${env:HOME}/.cargo/bin:"
    "/opt/homebrew/bin:/usr/local/bin:${env:PATH}"
)

DJANGO_MANAGE_OPTIONS = [
    "migrate",
    "createsuperuser",
    "collectstatic",
    "shell_plus",
    "makemigrations",
    "purge_image_renditions",
]

DEV_INFO_YAML = "docker/envs/dev/info.yaml"
FULL_INFO_YAML = "docker/envs/full/info.yaml"


def _site_origin_py(info_yaml: str) -> str:
    return (
        "import yaml; "
        f"print(yaml.safe_load(open('{info_yaml}'))['site_origin'])"
    )


def _start_stack_hint_py(info_yaml: str, open_task_label: str) -> str:
    return (
        "import yaml; "
        f"o=yaml.safe_load(open('{info_yaml}'))['site_origin']; "
        f"print(f'\\nSite: {{o}}\\nOpen with task: {open_task_label}')"
    )


class WorkflowKind(str, Enum):
    DOCKER = "docker"


def _task_options() -> dict[str, Any]:
    return {
        "cwd": "${workspaceFolder}",
        "env": {"PATH": PATH_ENV},
    }


def _presentation(
    *,
    panel: str = "shared",
    focus: bool = True,
    group: str | None = None,
) -> dict[str, Any]:
    pres: dict[str, Any] = {
        "reveal": "always",
        "panel": panel,
    }
    if focus:
        pres["focus"] = True
    if group:
        pres["group"] = group
    return pres


def _shell_task(
    label: str,
    command: str,
    *,
    panel: str = "shared",
    focus: bool = True,
    group: str | None = None,
    osx_command: str | None = None,
    linux_command: str | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "label": label,
        "type": "shell",
        "options": _task_options(),
        "presentation": _presentation(panel=panel, focus=focus, group=group),
        "problemMatcher": [],
    }
    if osx_command is not None or linux_command is not None:
        if osx_command is not None:
            task["osx"] = {"command": osx_command}
        if linux_command is not None:
            task["linux"] = {"command": linux_command}
        task["command"] = command
    else:
        task["command"] = command
    return task


def _open_browser_task(label: str, info_yaml: str) -> dict[str, Any]:
    site_origin_py = _site_origin_py(info_yaml)
    open_osx = f'open "$(uv run python -c "{site_origin_py}")"'
    open_linux = f'xdg-open "$(uv run python -c "{site_origin_py}")"'
    return _shell_task(
        label,
        open_linux,
        osx_command=open_osx,
        linux_command=open_linux,
    )


def django_manage_inputs() -> list[dict[str, Any]]:
    return [
        {
            "id": "djangoManageCommand",
            "type": "pickString",
            "description": "manage.py command",
            "options": DJANGO_MANAGE_OPTIONS,
            "default": "migrate",
        },
        {
            "id": "djangoManageArgs",
            "type": "promptString",
            "description": "Extra arguments for manage.py (optional; leave empty if none)",
            "default": "",
        },
    ]


def _dev_tasks(*, include_full: bool) -> list[dict[str, Any]]:
    dev_hint = _start_stack_hint_py(DEV_INFO_YAML, "Dev: Open site in browser")
    tasks: list[dict[str, Any]] = [
        _shell_task(
            "Dev: Start stack",
            f"uv run dk dev up -d && uv run python -c \"{dev_hint}\"",
            focus=True,
        ),
        _shell_task("Dev: Stop stack", "uv run dk dev down"),
        _shell_task("Dev: Show status", "uv run dk dev ps"),
        _shell_task(
            "Dev: View logs",
            "uv run dk dev logs -f --tail=200",
            panel="dedicated",
            focus=False,
        ),
        _open_browser_task("Dev: Open site in browser", DEV_INFO_YAML),
        _shell_task(
            "Dev: Restore database from backup",
            "uv run dk dev -y db restore",
        ),
        _shell_task(
            "Dev: Restore media files from backup",
            "uv run dk dev -y files restore",
        ),
        _shell_task(
            "Dev: Run Django command",
            "uv run dk dev manage ${input:djangoManageCommand} ${input:djangoManageArgs}",
        ),
        _shell_task(
            "Dev: Wipe data (destructive)",
            "uv run dk dev wipe",
        ),
    ]
    if include_full:
        full_hint = _start_stack_hint_py(FULL_INFO_YAML, "Full: Open site in browser")
        tasks.extend(
            [
                _shell_task(
                    "Full: Start stack",
                    f"uv run dk full up -d && uv run python -c \"{full_hint}\"",
                    focus=True,
                ),
                _shell_task("Full: Stop stack", "uv run dk full down"),
                _shell_task("Full: Show status", "uv run dk full ps"),
                _shell_task(
                    "Full: View logs",
                    "uv run dk full logs -f --tail=200",
                    panel="dedicated",
                    focus=False,
                ),
                _open_browser_task("Full: Open site in browser", FULL_INFO_YAML),
                _shell_task(
                    "Full: Trust HTTPS certificate (macOS)",
                    "uv run dk full trust-caddy-cert",
                ),
                _shell_task(
                    "Full: Restore database from backup",
                    "uv run dk full -y db restore",
                ),
                _shell_task(
                    "Full: Restore media files from backup",
                    "uv run dk full -y files restore",
                ),
                _shell_task(
                    "Full: Run Django command",
                    "uv run dk full manage ${input:djangoManageCommand} ${input:djangoManageArgs}",
                ),
            ]
        )
    return tasks


def build_extensions_json() -> dict[str, Any]:
    return {
        MANAGED_MARKER_KEY: SETUP_VSCODE_GENERATOR_VERSION,
        "recommendations": ["ms-azuretools.vscode-containers"],
    }


def build_settings_json() -> dict[str, Any]:
    return {
        MANAGED_MARKER_KEY: SETUP_VSCODE_GENERATOR_VERSION,
        "terminal.integrated.env.osx": {"PATH": PATH_ENV},
        "containers.containerClient": "com.microsoft.visualstudio.containers.docker",
        "containers.orchestratorClient": (
            "com.microsoft.visualstudio.orchestrators.dockercompose"
        ),
        "containers.containerCommand": "/usr/local/bin/docker",
        "containers.commands.composeUp": (
            "${composeCommand} ${configurationFile} up ${detached} ${build}"
        ),
    }


def build_tasks_json(
    workflow: WorkflowKind,
    *,
    include_full: bool = True,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Return a VS Code tasks.json document."""
    del workflow  # only docker workflow is supported
    tasks = _dev_tasks(include_full=include_full)
    return {
        "version": "2.0.0",
        MANAGED_MARKER_KEY: SETUP_VSCODE_GENERATOR_VERSION,
        "inputs": django_manage_inputs(),
        "tasks": tasks,
    }
