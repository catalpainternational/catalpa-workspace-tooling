"""Key-level defaults inside ``ops:`` and ``stack:`` (#153).

A project that uses ``ops:`` only for local database work should not have to invent
deploy-host values. Universal conventions are derived from ``project.name``; values that
differ per project are optional and the command that needs one names it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from catalpa_tooling.config import ProjectConfigError, load_project_config

_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "minimal_native_project"

_OPS_LOCAL_DB_ONLY = """
ops:
  pgbackrest:
    postgres_conf: 30-archive.conf
    pgbackrest_conf: 50-managed.conf
    default_registry: ghcr.io/example
"""

_STACK_MINIMAL = """
stack:
  compose_project_default: app_compose
  images:
    components:
      web: app-web
      proxy: app-proxy
      db: app-db
  healthcheck:
    url: http://127.0.0.1:8000/health/
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    shutil.copytree(_FIXTURE_ROOT, tmp_path, dirs_exist_ok=True)
    return tmp_path


def _append(project: Path, text: str) -> None:
    manifest = project / "tooling.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + text, encoding="utf-8")


def test_deploy_host_fields_derive_from_project_name(project: Path) -> None:
    _append(project, _OPS_LOCAL_DB_ONLY)
    ops = load_project_config(project).ops

    assert ops.install_prefix == "/opt/minimal-native"
    assert ops.config_dir == "/etc/minimal-native"
    assert ops.systemd_unit_prefix == "minimal-native-"
    assert ops.transfer_workdir == ".minimal-native-transfer"


def test_project_specific_fields_are_unset_not_invented(project: Path) -> None:
    _append(project, _OPS_LOCAL_DB_ONLY)
    ops = load_project_config(project).ops

    assert ops.zabbix is None
    assert ops.default_db_container is None
    assert ops.systemd_units.pgbackrest == ()
    assert ops.systemd_units.restic == ()
    assert ops.systemd_units.timers_enable_pgbackrest == ()


def test_explicit_values_win(project: Path) -> None:
    _append(
        project,
        _OPS_LOCAL_DB_ONLY
        + """  install_prefix: /srv/app
  config_dir: /srv/app/etc
  systemd_unit_prefix: app-
  transfer_workdir: .xfer
  default_db_container: app-db-1
  zabbix:
    unit_name: app-zabbix-agent2.service
    userparams_file: 99-app.conf
""",
    )
    ops = load_project_config(project).ops

    assert ops.install_prefix == "/srv/app"
    assert ops.config_dir == "/srv/app/etc"
    assert ops.systemd_unit_prefix == "app-"
    assert ops.transfer_workdir == ".xfer"
    assert ops.default_db_container == "app-db-1"
    assert ops.require_zabbix().unit_name == "app-zabbix-agent2.service"


def test_missing_zabbix_fails_when_the_zabbix_commands_run(project: Path) -> None:
    from catalpa_tooling import zabbix_systemd

    _append(project, _OPS_LOCAL_DB_ONLY + _STACK_MINIMAL)
    config = load_project_config(project)

    with pytest.raises(ProjectConfigError, match="ops.zabbix"):
        zabbix_systemd._apply_config_globals(config)


def test_transfer_workdir_default_is_validated(project: Path) -> None:
    _append(project, _OPS_LOCAL_DB_ONLY + "  transfer_workdir: ../escape\n")

    with pytest.raises(ProjectConfigError, match="ops.transfer_workdir"):
        load_project_config(project)


def test_stack_service_and_registry_defaults(project: Path) -> None:
    _append(project, _STACK_MINIMAL)
    stack = load_project_config(project).stack

    assert (stack.services.web, stack.services.proxy, stack.services.db) == (
        "django",
        "caddy",
        "db",
    )
    assert stack.images.registry_key == "image_registry"
    assert stack.healthcheck.service == "django"


def test_healthcheck_service_follows_a_custom_web_service(project: Path) -> None:
    _append(project, _STACK_MINIMAL + "  services:\n    web: web\n")
    stack = load_project_config(project).stack

    assert stack.services.web == "web"
    assert stack.services.proxy == "caddy"
    assert stack.healthcheck.service == "web"


def test_stack_components_and_compose_project_stay_required(project: Path) -> None:
    _append(project, "\nstack:\n  images:\n    components: {}\n  healthcheck:\n    url: /\n")

    with pytest.raises(ProjectConfigError, match="compose_project_default"):
        load_project_config(project)


_DEPLOY_PATHS = """
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.yml
"""


def _with_deploy(project: Path, images_yaml: str | None) -> None:
    manifest = project / "tooling.yaml"
    text = manifest.read_text(encoding="utf-8").replace(
        "  fetch_db_dump: dumps/app.custom\n",
        "  fetch_db_dump: dumps/app.custom\n" + _DEPLOY_PATHS.lstrip("\n"),
    )
    manifest.write_text(text, encoding="utf-8")
    (project / "docker" / "envs").mkdir(parents=True, exist_ok=True)
    if images_yaml is not None:
        (project / "docker" / "images.yaml").write_text(images_yaml, encoding="utf-8")


def test_empty_ops_mapping_is_adopted_with_every_default(project: Path) -> None:
    _append(project, "\nops: {}\n")
    config = load_project_config(project)

    assert config.has_ops is True
    pg = config.ops.pgbackrest
    assert pg.postgres_conf == "30-minimal-native-pgbackrest-archive.conf"
    assert pg.pgbackrest_conf == "50-minimal-native-managed.conf"
    assert pg.default_registry is None
    assert pg.data_volume == "postgres_data"


def test_db_image_registry_falls_back_to_images_yaml(project: Path) -> None:
    from catalpa_tooling.pgbackrest_volume_config import postgres_image_from_env

    _with_deploy(project, "image_registry: ghcr.io/example/app/\n")
    _append(project, "\nops: {}\n" + _STACK_MINIMAL)
    config = load_project_config(project)

    assert postgres_image_from_env({}, config=config) == "ghcr.io/example/app/app-db:latest"
    assert (
        postgres_image_from_env({"STACK_IMAGE_REGISTRY": "reg.local"}, config=config)
        == "reg.local/app-db:latest"
    )


def test_explicit_default_registry_still_wins_over_images_yaml(project: Path) -> None:
    from catalpa_tooling.pgbackrest_volume_config import postgres_image_from_env

    _with_deploy(project, "image_registry: ghcr.io/from-images\n")
    _append(project, _OPS_LOCAL_DB_ONLY + _STACK_MINIMAL)
    config = load_project_config(project)

    assert postgres_image_from_env({}, config=config) == "ghcr.io/example/app-db:latest"


def test_db_image_without_any_registry_names_the_field(project: Path) -> None:
    from catalpa_tooling.pgbackrest_volume_config import postgres_image_from_env

    _with_deploy(project, None)
    _append(project, "\nops: {}\n" + _STACK_MINIMAL)
    config = load_project_config(project)

    with pytest.raises(ProjectConfigError, match="ops.pgbackrest.default_registry"):
        postgres_image_from_env({}, config=config)


@pytest.mark.parametrize(("only", "kind"), [("pgbackrest", "pgbackrest"), ("restic", "restic")])
def test_install_systemd_names_the_empty_unit_list(
    project: Path, capsys: pytest.CaptureFixture, only: str, kind: str
) -> None:
    from unittest.mock import patch

    from catalpa_tooling import systemd_remote_install

    _with_deploy(project, None)
    _append(project, "\nops: {}\n" + _STACK_MINIMAL)
    config = load_project_config(project)

    with patch.object(
        systemd_remote_install, "resolve_env_with_compose_project", return_value={}
    ):
        rc = systemd_remote_install.cmd_install_systemd_backups(
            config,
            {},
            "ssh://root@203.0.113.5",
            "prod",
            ["--dry-run"],
            global_dry_run=True,
            yes=True,
            fixed_only=only,
        )

    assert rc == 1
    assert f"ops.systemd_units.{kind}" in capsys.readouterr().err
