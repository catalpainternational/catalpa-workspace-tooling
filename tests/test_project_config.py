"""Tests for catalpa_tooling.config."""

from pathlib import Path

import pytest

from catalpa_tooling.config import ProjectConfigError, load_project_config
from catalpa_tooling.config import tooling_path_for_repo


def test_load_minimal_project(minimal_project) -> None:
    assert minimal_project.meta.name == "minimal"
    assert minimal_project.ops.pgbackrest.restore_temp_prefix == "app_pgrestore_"
    assert minimal_project.backend_dir == minimal_project.repo_root / "backend"
    assert minimal_project.paths.scripts == ("scripts",)
    assert minimal_project.scripts_dir == minimal_project.repo_root / "scripts"
    assert minimal_project.image_component("web") == "app-web"
    assert minimal_project.stack.compose_project_default == "app_compose"
    assert minimal_project.ops.restic.data_volume == "django_media"
    assert minimal_project.credentials_optional_for_env("local")
    assert minimal_project.credentials_optional_for_env("local_foo")
    assert not minimal_project.credentials_optional_for_env("staging")


def test_credentials_optional_envs(minimal_project) -> None:
    assert minimal_project.ops.install_prefix == "/opt/app"


def test_restore_temp_prefix_defaults_to_project_name(tmp_path: Path, isolated_tooling: None) -> None:
    (tmp_path / "pyproject.toml").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(
        """
project:
  name: myapp
paths:
  backend: .
  frontend: f
  scripts: s
  env_local: .env
  email_backend_dir: e
  fetch_db_dump: d
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
stack:
  compose_project_default: p
  services: {web: w, proxy: p, db: d}
  images:
    registry_key: image_registry
    components: {web: w, proxy: p, db: d}
  healthcheck: {service: w, url: http://localhost/healthz}
ops:
  install_prefix: /opt/x
  config_dir: /etc/x
  systemd_unit_prefix: x-
  transfer_workdir: .x
  default_db_container: x_db
  pgbackrest:
    postgres_conf: a.conf
    pgbackrest_conf: b.conf
    default_registry: reg
  zabbix:
    unit_name: u.service
    userparams_file: 99.conf
  systemd_units:
    pgbackrest: []
    restic: []
""",
        encoding="utf-8",
    )
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "images.yaml").write_text("image_registry: x\n", encoding="utf-8")
    cfg = load_project_config(tmp_path)
    assert cfg.ops.pgbackrest.restore_temp_prefix == "myapp_pgrestore_"


def test_paths_scripts_accepts_list(tmp_path: Path, isolated_tooling: None) -> None:
    (tmp_path / "pyproject.toml").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(
        """
project:
  name: myapp
paths:
  backend: .
  frontend: bero
  scripts:
    - scripts
    - bero/docker/postgres/scripts
  env_local: .env
  email_backend_dir: e
  fetch_db_dump: d
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
stack:
  compose_project_default: p
  services: {web: w, proxy: p, db: d}
  images:
    registry_key: image_registry
    components: {web: w, proxy: p, db: d}
  healthcheck: {service: w, url: http://localhost/healthz}
ops:
  install_prefix: /opt/x
  config_dir: /etc/x
  systemd_unit_prefix: x-
  transfer_workdir: .x
  default_db_container: x_db
  pgbackrest:
    postgres_conf: a.conf
    pgbackrest_conf: b.conf
    default_registry: reg
  zabbix:
    unit_name: u.service
    userparams_file: 99.conf
  systemd_units:
    pgbackrest: []
    restic: []
""",
        encoding="utf-8",
    )
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "images.yaml").write_text("image_registry: x\n", encoding="utf-8")
    cfg = load_project_config(tmp_path)
    assert cfg.paths.scripts == ("scripts", "bero/docker/postgres/scripts")
    assert len(cfg.scripts_dirs) == 2


def test_missing_required_key(tmp_path: Path, isolated_tooling: None) -> None:
    (tmp_path / "pyproject.toml").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text("project:\n  name: x\n", encoding="utf-8")
    with pytest.raises(ProjectConfigError, match="paths"):
        load_project_config(tmp_path)


def test_invalid_path_traversal(tmp_path: Path, isolated_tooling: None) -> None:
    (tmp_path / "pyproject.toml").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(
        """
project:
  name: x
paths:
  backend: ../escape
  frontend: f
  scripts: s
  env_local: .env
  email_backend_dir: e
  fetch_db_dump: d
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
stack:
  compose_project_default: p
  services: {web: w, proxy: p, db: d}
  images:
    registry_key: image_registry
    components: {web: w, proxy: p, db: d}
  healthcheck: {service: w, url: http://localhost/healthz}
ops:
  install_prefix: /opt/x
  config_dir: /etc/x
  systemd_unit_prefix: x-
  transfer_workdir: .x
  default_db_container: x_db
  pgbackrest:
    postgres_conf: a.conf
    pgbackrest_conf: b.conf
    default_registry: reg
    restore_temp_prefix: pre_
  zabbix:
    unit_name: u.service
    userparams_file: 99.conf
  systemd_units:
    pgbackrest: []
    restic: []
""",
        encoding="utf-8",
    )
    with pytest.raises(ProjectConfigError, match="Invalid relative path"):
        load_project_config(tmp_path)


def test_tooling_config_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    manifest = other / "tooling.yaml"
    manifest.write_bytes((Path(__file__).parent / "fixtures" / "minimal_project" / "tooling.yaml").read_bytes())
    (other / "pyproject.toml").write_text("n=1\n", encoding="utf-8")
    (other / "docker").mkdir()
    (other / "docker" / "images.yaml").write_text("image_registry: x\n", encoding="utf-8")
    monkeypatch.setenv("TOOLING_CONFIG", str(manifest))
    cfg = load_project_config(tmp_path)
    assert cfg.repo_root == other.resolve()


def test_digitalocean_block_optional(minimal_project) -> None:
    assert minimal_project.digitalocean is None


def test_digitalocean_block_parses(tmp_path: Path, isolated_tooling: None) -> None:
    from tests.helpers import write_minimal_tooling_tree

    write_minimal_tooling_tree(tmp_path)
    tooling = tmp_path / "tooling.yaml"
    text = tooling.read_text(encoding="utf-8")
    tooling.write_text(
        text
        + """
digitalocean:
  project_name: staging
  context: team-a
  timezone: Asia/Dili
  region: sgp1
  size: s-2vcpu-4gb
  image: ubuntu-24-04-x64
  ssh_keys:
    - "aa:bb:cc:dd"
""",
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    assert cfg.digitalocean is not None
    assert cfg.digitalocean.project_name == "staging"
    assert cfg.digitalocean.context == "team-a"
    assert cfg.digitalocean.timezone == "Asia/Dili"
    assert cfg.digitalocean.region == "sgp1"
    assert cfg.digitalocean.size == "s-2vcpu-4gb"
    assert cfg.digitalocean.image == "ubuntu-24-04-x64"
    assert cfg.digitalocean.ssh_keys == ("aa:bb:cc:dd",)
    assert cfg.digitalocean.monitoring is True


def test_digitalocean_monitoring_false(tmp_path: Path, isolated_tooling: None) -> None:
    from tests.helpers import write_minimal_tooling_tree

    write_minimal_tooling_tree(tmp_path)
    tooling = tmp_path / "tooling.yaml"
    tooling.write_text(
        tooling.read_text(encoding="utf-8")
        + """
digitalocean:
  project_name: staging
  monitoring: false
""",
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    assert cfg.digitalocean is not None
    assert cfg.digitalocean.monitoring is False


def test_digitalocean_rejects_both_project_keys(tmp_path: Path, isolated_tooling: None) -> None:
    from tests.helpers import write_minimal_tooling_tree

    write_minimal_tooling_tree(tmp_path)
    tooling = tmp_path / "tooling.yaml"
    text = tooling.read_text(encoding="utf-8")
    tooling.write_text(
        text
        + """
digitalocean:
  project_name: a
  project_id: bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
""",
        encoding="utf-8",
    )
    with pytest.raises(ProjectConfigError, match="only one"):
        load_project_config(tmp_path)


def test_systemd_units_invalid_suffix(tmp_path: Path, isolated_tooling: None) -> None:
    from tests.helpers import write_minimal_tooling_tree

    write_minimal_tooling_tree(tmp_path)
    tooling = tmp_path / "tooling.yaml"
    text = tooling.read_text(encoding="utf-8")
    tooling.write_text(
        text.replace(
            "app-pgbackrest-backup-full.service",
            "app-unknown-backup.service",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProjectConfigError, match="Unknown systemd unit"):
        load_project_config(tmp_path)


def test_restic_invalid_data_volume(tmp_path: Path, isolated_tooling: None) -> None:
    from tests.helpers import write_minimal_tooling_tree

    write_minimal_tooling_tree(tmp_path)
    tooling = tmp_path / "tooling.yaml"
    text = tooling.read_text(encoding="utf-8")
    tooling.write_text(
        text.replace(
            "  zabbix:",
            "  restic:\n    data_volume: bad/volume\n  zabbix:",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProjectConfigError, match="ops.restic.data_volume"):
        load_project_config(tmp_path)


def test_indmo_reference_tooling_snapshot(tmp_path: Path, isolated_tooling: None) -> None:
    """Guard the bundled INDMO reference manifest (consumer parity check)."""
    ref = Path(__file__).parent / "fixtures" / "indmo_reference_tooling.yaml"
    (tmp_path / "pyproject.toml").write_text("name = indmo\n", encoding="utf-8")
    (tmp_path / "tooling.yaml").write_bytes(ref.read_bytes())
    cfg = load_project_config(tmp_path)
    assert cfg.paths.backend == "django_backend"
    assert cfg.stack.compose_project_default == "pas_indmo"
    assert cfg.image_component("web") == "indmo-django"
    assert cfg.ops.install_prefix == "/opt/indmo"
