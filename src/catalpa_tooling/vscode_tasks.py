"""Build VS Code ``tasks.json`` content for Catalpa tooling repos."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.local_proxy import local_proxy_enabled
from catalpa_tooling.site_origin import primary_site_origin_from_info

SETUP_VSCODE_GENERATOR_VERSION = "8"
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

DEV_CURSOR_BROWSER_INPUT = "devOpenCursorBrowser"
FULL_CURSOR_BROWSER_INPUT = "fullOpenCursorBrowser"
CURSOR_BROWSER_COMMAND = "workbench.action.openBrowserEditor"


def _site_origin_py(info_yaml: str) -> str:
    # site_origin may be a string or YAML list; open only the primary URL.
    return (
        "import yaml; "
        "from catalpa_tooling.site_origin import primary_site_origin_from_info; "
        f"print(primary_site_origin_from_info(yaml.safe_load(open('{info_yaml}')) or {{}}))"
    )


def _start_stack_hint_py(
    info_yaml: str,
    os_browser_label: str,
    cursor_browser_label: str,
) -> str:
    return (
        "import yaml; "
        "from catalpa_tooling.site_origin import parse_site_origins_from_info; "
        f"info=yaml.safe_load(open('{info_yaml}')) or {{}}; "
        "o=' '.join(parse_site_origins_from_info(info)); "
        f"print(f'\\nSite: {{o}}\\nOpen in OS browser: {os_browser_label}\\n"
        f"Open in Cursor: {cursor_browser_label}'); "
        "from catalpa_tooling.dev_lan_access import dev_lan_access_enabled; "
        "dev_lan_access_enabled(info) and print('LAN testing: task Dev: Show LAN URLs')"
    )


def _read_site_origin(info_yaml: Path) -> str:
    if not info_yaml.is_file():
        return ""
    info = yaml.safe_load(info_yaml.read_text(encoding="utf-8")) or {}
    return primary_site_origin_from_info(info) or ""


def _local_proxy_enabled(info_yaml: Path) -> bool:
    if not info_yaml.is_file():
        return False
    info = yaml.safe_load(info_yaml.read_text(encoding="utf-8")) or {}
    return local_proxy_enabled(info)


def _cursor_browser_input(input_id: str, url: str) -> dict[str, Any]:
    return {
        "id": input_id,
        "type": "command",
        "command": CURSOR_BROWSER_COMMAND,
        "args": {"url": url},
    }


def _open_cursor_browser_task(label: str, input_id: str) -> dict[str, Any]:
    # Command-type inputs must run inside a shell task (echo); using "command"
    # alone makes VS Code/Cursor treat ${input:…} as a shell executable path.
    return _shell_task(
        label,
        f"echo ${{input:{input_id}}}",
        panel="shared",
        focus=False,
    ) | {
        "presentation": {
            "reveal": "never",
            "panel": "shared",
            "focus": False,
            "echo": False,
        },
    }


def _cursor_browser_inputs(
    deploy_envs_dir: Path,
    *,
    include_full: bool,
) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    dev_url = _read_site_origin(deploy_envs_dir / "dev" / "info.yaml")
    if dev_url:
        inputs.append(_cursor_browser_input(DEV_CURSOR_BROWSER_INPUT, dev_url))
    if include_full:
        full_url = _read_site_origin(deploy_envs_dir / "full" / "info.yaml")
        if full_url:
            inputs.append(_cursor_browser_input(FULL_CURSOR_BROWSER_INPUT, full_url))
    return inputs


def _dev_lan_urls_py(info_yaml: str) -> str:
    return (
        "import yaml; "
        f"from catalpa_tooling.dev_lan_access import print_dev_lan_urls; "
        f"print_dev_lan_urls(yaml.safe_load(open('{info_yaml}')) or {{}})"
    )


def _first_dev_lan_url_py(info_yaml: str) -> str:
    return (
        "import yaml; "
        "from catalpa_tooling.dev_lan_access import format_dev_lan_urls; "
        f"urls=format_dev_lan_urls(yaml.safe_load(open('{info_yaml}')) or {{}}); "
        "print(urls[0] if urls else '', end='')"
    )


def _open_dev_lan_browser_task(label: str, info_yaml: str) -> dict[str, Any]:
    url_py = _first_dev_lan_url_py(info_yaml)
    open_osx = f'open "$(uv run python -c "{url_py}")"'
    open_linux = f'xdg-open "$(uv run python -c "{url_py}")"'
    return _shell_task(
        label,
        open_linux,
        osx_command=open_osx,
        linux_command=open_linux,
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


def _dev_tasks(
    *,
    include_full: bool,
    deploy_envs_dir: Path | None = None,
) -> list[dict[str, Any]]:
    dev_hint = _start_stack_hint_py(
        DEV_INFO_YAML,
        "Dev: Open site in browser",
        "Dev: Open site in Cursor browser",
    )
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
        *(
            [
                _open_cursor_browser_task(
                    "Dev: Open site in Cursor browser",
                    DEV_CURSOR_BROWSER_INPUT,
                )
            ]
            if deploy_envs_dir
            and _read_site_origin(deploy_envs_dir / "dev" / "info.yaml")
            else []
        ),
        *(
            [
                # Machine-wide CA shared by every project's local dev proxy, so
                # this is not prefixed like the per-env "Dev:"/"Full:" tasks.
                _shell_task(
                    "Trust Catalpa local dev CA",
                    "uv run dk dev trust-caddy-cert",
                )
            ]
            if deploy_envs_dir
            and _local_proxy_enabled(deploy_envs_dir / "dev" / "info.yaml")
            else []
        ),
        _shell_task(
            "Dev: Show LAN URLs",
            f'uv run python -c "{_dev_lan_urls_py(DEV_INFO_YAML)}"',
        ),
        _open_dev_lan_browser_task("Dev: Open site on LAN", DEV_INFO_YAML),
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
        full_hint = _start_stack_hint_py(
            FULL_INFO_YAML,
            "Full: Open site in browser",
            "Full: Open site in Cursor browser",
        )
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
                *(
                    [
                        _open_cursor_browser_task(
                            "Full: Open site in Cursor browser",
                            FULL_CURSOR_BROWSER_INPUT,
                        )
                    ]
                    if deploy_envs_dir
                    and _read_site_origin(deploy_envs_dir / "full" / "info.yaml")
                    else []
                ),
                _shell_task(
                    "Full: Trust HTTPS certificate",
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
    }


def build_tasks_json(
    workflow: WorkflowKind,
    *,
    include_full: bool = True,
    deploy_envs_dir: Path | None = None,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Return a VS Code tasks.json document."""
    del workflow  # only docker workflow is supported
    tasks = _dev_tasks(include_full=include_full, deploy_envs_dir=deploy_envs_dir)
    inputs = django_manage_inputs()
    if deploy_envs_dir is not None:
        inputs.extend(_cursor_browser_inputs(deploy_envs_dir, include_full=include_full))
    return {
        "version": "2.0.0",
        MANAGED_MARKER_KEY: SETUP_VSCODE_GENERATOR_VERSION,
        "inputs": inputs,
        "tasks": tasks,
    }
