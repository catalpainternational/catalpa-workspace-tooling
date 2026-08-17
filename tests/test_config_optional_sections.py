"""``stack:``, ``ops:``, and ``paths.deploy`` are optional; access fails lazily and clearly."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from catalpa_tooling.config import ProjectConfigError, load_project_config

_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "minimal_native_project"


@pytest.fixture
def native_only_project(tmp_path: Path) -> Path:
    shutil.copytree(_FIXTURE_ROOT, tmp_path, dirs_exist_ok=True)
    return tmp_path


def test_manifest_without_stack_ops_or_deploy_loads(native_only_project: Path) -> None:
    config = load_project_config(native_only_project)

    assert config.meta.name == "minimal-native"
    assert config.backend_dir == native_only_project / "backend"
    assert config.frontend_dir == native_only_project / "frontend"
    assert config.scripts_dir == native_only_project / "scripts"


def test_presence_flags_report_missing_sections(native_only_project: Path) -> None:
    config = load_project_config(native_only_project)

    assert config.has_stack is False
    assert config.has_ops is False
    assert config.has_deploy_paths is False


def test_full_manifest_still_reports_sections_present(tmp_path: Path) -> None:
    from tests.helpers import write_minimal_tooling_tree

    write_minimal_tooling_tree(tmp_path)
    config = load_project_config(tmp_path)

    assert config.has_stack is True
    assert config.has_ops is True
    assert config.has_deploy_paths is True
    assert config.stack.services.web == "web"
    assert config.ops.install_prefix == "/opt/app"
    assert config.compose_prod == "compose.yml"


@pytest.mark.parametrize(
    ("attribute", "section", "needed_for"),
    [
        ("stack", "stack", "dk build"),
        ("ops", "ops", "dk transfer"),
    ],
)
def test_missing_section_raises_pointed_error(
    native_only_project: Path, attribute: str, section: str, needed_for: str
) -> None:
    config = load_project_config(native_only_project)

    with pytest.raises(ProjectConfigError) as excinfo:
        getattr(config, attribute)

    message = str(excinfo.value)
    assert f"`{section}:` section" in message
    assert needed_for in message
    assert "tests/fixtures/minimal_project/tooling.yaml" in message


def test_missing_deploy_paths_raises_pointed_error(native_only_project: Path) -> None:
    config = load_project_config(native_only_project)

    with pytest.raises(ProjectConfigError) as excinfo:
        _ = config.deploy_envs_dir

    assert "`paths.deploy:` section" in str(excinfo.value)


def test_stack_helpers_surface_the_same_error(native_only_project: Path) -> None:
    """Derived accessors must not mask the missing-section error with an AttributeError."""
    config = load_project_config(native_only_project)

    with pytest.raises(ProjectConfigError):
        config.stack_service("web")
    with pytest.raises(ProjectConfigError):
        config.image_component("web")
    with pytest.raises(ProjectConfigError):
        _ = config.compose_prod


def test_dk_parser_builds_without_deploy_sections(native_only_project: Path) -> None:
    """The `dk` parser must build so engine-agnostic subcommands stay reachable."""
    from catalpa_tooling.dk_parser import build_dk_parser

    config = load_project_config(native_only_project)
    parser = build_dk_parser(config)

    ns = parser.parse_args(["cut-release", "beta"])
    assert ns.dk_command == "cut-release"


def test_no_deploy_paths_means_no_environments(native_only_project: Path) -> None:
    from catalpa_tooling.remote_deploy import list_dk_env_names

    assert list_dk_env_names(load_project_config(native_only_project)) == []


def test_run_cli_reports_config_errors_without_traceback(capsys: pytest.CaptureFixture) -> None:
    from catalpa_tooling.cli_interrupt import run_cli

    def boom() -> None:
        raise ProjectConfigError("no `stack:` section")

    with pytest.raises(SystemExit) as excinfo:
        run_cli(boom, label="dk")

    assert excinfo.value.code == 1
    assert capsys.readouterr().err.strip() == "dk: no `stack:` section"


def test_malformed_stack_section_still_rejected(native_only_project: Path) -> None:
    """Optional means absent-is-fine, not present-but-wrong-is-fine."""
    manifest = native_only_project / "tooling.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\nstack: not-a-mapping\n", encoding="utf-8"
    )

    with pytest.raises(ProjectConfigError, match="stack"):
        load_project_config(native_only_project)
