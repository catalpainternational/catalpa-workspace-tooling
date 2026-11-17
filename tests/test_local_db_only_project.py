"""A manifest with no deploy-host values reaches the local database code paths (#153).

partisipa-import had to put placeholder install_prefix / systemd values in `ops:` just so
`dk dev db restore` would load. This fixture leaves them all out.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from catalpa_tooling.config import ProjectConfigError, load_project_config

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "local_db_only_project"


@pytest.fixture
def config(tmp_path: Path, isolated_tooling: None, monkeypatch: pytest.MonkeyPatch):
    shutil.copytree(_FIXTURE, tmp_path, dirs_exist_ok=True)
    monkeypatch.chdir(tmp_path)
    return load_project_config(tmp_path)


def test_manifest_loads_with_derived_deploy_host_values(config) -> None:
    assert config.ops.install_prefix == "/opt/localdb"
    assert config.ops.zabbix is None
    assert config.ops.post_db_restore.manage_commands == (("migrate", "--noinput"),)


def test_dev_context_loads_and_names_the_db_image(config) -> None:
    from catalpa_tooling.managed_deploy_env import load_managed_deploy_context
    from catalpa_tooling.pgbackrest_volume_config import postgres_image_from_env

    ctx = load_managed_deploy_context(config, "dev")
    assert ctx is not None

    image = postgres_image_from_env(ctx.env_add, config=config)
    assert image.startswith("ghcr.io/example/localdb/localdb-postgres:")


def test_pgbackrest_volume_config_dry_run_uses_derived_drop_in_names(
    config, capsys: pytest.CaptureFixture
) -> None:
    from catalpa_tooling.managed_deploy_env import load_managed_deploy_context
    from catalpa_tooling.pgbackrest_volume_config import materialize_configs

    ctx = load_managed_deploy_context(config, "dev")
    assert materialize_configs(ctx.env_add, dry_run=True, config=config) == 0
    assert config.ops.pgbackrest.postgres_conf == "30-localdb-pgbackrest-archive.conf"
    assert config.ops.pgbackrest.pgbackrest_conf == "50-localdb-managed.conf"


def test_zabbix_commands_name_the_missing_section(config) -> None:
    from catalpa_tooling import zabbix_systemd

    with pytest.raises(ProjectConfigError, match="ops.zabbix"):
        zabbix_systemd._apply_config_globals(config)
