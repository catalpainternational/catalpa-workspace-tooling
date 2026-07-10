"""Tests for catalpa_tooling.config."""

from pathlib import Path

import pytest

from catalpa_tooling.config import ProjectConfigError, _parse_reset_db, load_project_config
from catalpa_tooling.config import resolve_compliance_config, tooling_path_for_repo
from tests.helpers import write_minimal_tooling_tree


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


def test_restore_as_super_defaults_false_when_omitted() -> None:
    cfg = _parse_reset_db({"postgis": True})
    assert cfg.postgis is True
    assert cfg.restore_as_super is False


def test_restore_as_super_explicit_false_with_postgis() -> None:
    cfg = _parse_reset_db({"postgis": True, "restore_as_super": False})
    assert cfg.restore_as_super is False


def test_restore_as_super_false_when_postgis_false_and_omitted() -> None:
    cfg = _parse_reset_db({"postgis": False})
    assert cfg.restore_as_super is False


def test_restore_as_super_true_without_postgis_when_explicit() -> None:
    cfg = _parse_reset_db({"postgis": False, "restore_as_super": True})
    assert cfg.restore_as_super is True


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


def test_restic_invalid_backup_path(tmp_path: Path, isolated_tooling: None) -> None:
    from tests.helpers import write_minimal_tooling_tree

    write_minimal_tooling_tree(tmp_path)
    tooling = tmp_path / "tooling.yaml"
    text = tooling.read_text(encoding="utf-8")
    tooling.write_text(
        text.replace(
            "  zabbix:",
            "  restic:\n    backup_path: relative/path\n  zabbix:",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProjectConfigError, match="ops.restic.backup_path"):
        load_project_config(tmp_path)


def test_minimal_config_generalized_defaults(minimal_config) -> None:
    from catalpa_tooling.config import (
        DEFAULT_BUILD_PLACEHOLDERS,
        DEFAULT_DEV_LAN_DNS_SUFFIX,
        DEFAULT_DEV_SITE_ORIGIN_BASE,
        DEFAULT_ORIGIN_ENV_KEYS,
    )

    assert minimal_config.dev.site_origin_base == DEFAULT_DEV_SITE_ORIGIN_BASE
    assert minimal_config.dev.lan_dns_suffix == DEFAULT_DEV_LAN_DNS_SUFFIX
    assert minimal_config.stack.origin_env_keys == DEFAULT_ORIGIN_ENV_KEYS
    assert minimal_config.stack.build_placeholders == DEFAULT_BUILD_PLACEHOLDERS


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


def _write_compliance_tooling(target: Path, extra: str = "") -> None:
    write_minimal_tooling_tree(target)
    tooling = (target / "tooling.yaml").read_text(encoding="utf-8")
    block = f"""
compliance:
  project_license: AGPL-3.0-or-later
  license_files:
    - frontend/LICENSE
  python:
    lockfiles:
      - frontend/docker/uv.lock
  javascript:
    cwd: frontend
    lockfile: yarn.lock
    production_only: true
  bundled_assets:
    - path: frontend/src/fonts
      license_globs: ["*OFL*", "LICENSE*"]
  forbidden_spdx:
    - UNLICENSED
  warn_spdx:
    - GPL-3.0-only
    - UNKNOWN
  allow_strong_copyleft: true
  outputs:
    sbom_dir: compliance/sbom
    notices: compliance/THIRD_PARTY_NOTICES.md
{extra}"""
    (target / "tooling.yaml").write_text(tooling + block, encoding="utf-8")


def test_parse_compliance_section(tmp_path: Path, isolated_tooling: None) -> None:
    _write_compliance_tooling(tmp_path)
    config = load_project_config(tmp_path)
    assert config.compliance is not None
    compliance = config.compliance
    assert compliance.project_license == "AGPL-3.0-or-later"
    assert compliance.license_files == ("frontend/LICENSE",)
    assert compliance.python is not None
    assert compliance.python.lockfiles == ("frontend/docker/uv.lock",)
    assert compliance.javascript is not None
    assert compliance.javascript.cwd == "frontend"
    assert compliance.javascript.production_only is True
    assert len(compliance.bundled_assets) == 1
    assert compliance.allow_strong_copyleft is True
    assert compliance.outputs.sbom_dir == "compliance/sbom"


def test_compliance_outputs_default_when_omitted(tmp_path: Path, isolated_tooling: None) -> None:
    write_minimal_tooling_tree(tmp_path)
    tooling = (tmp_path / "tooling.yaml").read_text(encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(
        tooling
        + """
compliance:
  project_license: MIT
""",
        encoding="utf-8",
    )
    config = load_project_config(tmp_path)
    assert config.compliance is not None
    assert config.compliance.outputs.sbom_dir == "compliance/sbom"
    assert config.compliance.outputs.notices == "compliance/THIRD_PARTY_NOTICES.md"
    assert config.compliance.javascript is None


def test_compliance_missing_project_license_raises(tmp_path: Path, isolated_tooling: None) -> None:
    write_minimal_tooling_tree(tmp_path)
    tooling = (tmp_path / "tooling.yaml").read_text(encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(
        tooling
        + """
compliance:
  outputs:
    sbom_dir: compliance/sbom
    notices: compliance/THIRD_PARTY_NOTICES.md
""",
        encoding="utf-8",
    )
    with pytest.raises(ProjectConfigError, match="project_license"):
        load_project_config(tmp_path)


def test_resolve_inferred_compliance_without_section(tmp_path: Path, isolated_tooling: None) -> None:
    write_minimal_tooling_tree(tmp_path)
    (tmp_path / "frontend" / "yarn.lock").write_text("# lock\n", encoding="utf-8")
    config = load_project_config(tmp_path)
    assert config.compliance is None
    inferred = resolve_compliance_config(config)
    assert inferred is not None
    assert inferred.javascript is not None
    assert inferred.javascript.lockfile == "yarn.lock"


def test_resolve_inferred_compliance_prefers_pnpm_lockfile(
    tmp_path: Path, isolated_tooling: None
) -> None:
    write_minimal_tooling_tree(tmp_path)
    (tmp_path / "frontend" / "yarn.lock").write_text("# yarn\n", encoding="utf-8")
    (tmp_path / "frontend" / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\nimporters:\n  .: {}\n",
        encoding="utf-8",
    )
    config = load_project_config(tmp_path)
    inferred = resolve_compliance_config(config)
    assert inferred is not None
    assert inferred.javascript is not None
    assert inferred.javascript.lockfile == "pnpm-lock.yaml"
