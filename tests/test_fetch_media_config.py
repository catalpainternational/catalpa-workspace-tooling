"""Tests for fetch media config and rsync helpers."""

from pathlib import Path
import pytest
import yaml

from catalpa_tooling.config import (
    DEFAULT_FETCH_MEDIA_DK_ENV,
    FetchMediaConfig,
    FetchMediaLegacyConfig,
    ProjectConfigError,
    load_project_config,
)
from catalpa_tooling.fetch_media import dk_info_fetch_media_defaults, run_fetch_media
from catalpa_tooling.media_rsync import docker_volume_mountpoint_ssh, ssh_target_from_host
from catalpa_tooling.native_parser import build_native_parser


def _write_minimal_tooling(tmp_path: Path, *, compose_default: str = "app_compose") -> None:
    (tmp_path / "pyproject.toml").write_text("n=1\n", encoding="utf-8")
    (tmp_path / "tooling.yaml").write_text(
        f"""
project:
  name: test
paths:
  backend: backend
  frontend: frontend
  scripts: scripts
  env_local: .env.local
  email_backend_dir: email_out
  fetch_db_dump: dump
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yml
    dev_compose: compose.dev.yaml
stack:
  compose_project_default: {compose_default}
  services: {{web: w, proxy: p, db: d}}
  images:
    registry_key: image_registry
    components: {{web: w-img, proxy: p-img, db: d-img}}
  healthcheck: {{service: w, url: http://localhost/healthz}}
ops:
  install_prefix: /opt/app
  config_dir: /etc/app
  systemd_unit_prefix: app-
  transfer_workdir: .xfer
  default_db_container: app_db
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


def test_default_fetch_media_dk_env_without_dev_section(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    assert cfg.native.fetch_media.dk_env == DEFAULT_FETCH_MEDIA_DK_ENV
    assert cfg.native.fetch_media.dest == "media"
    assert cfg.native.fetch_media.legacy is None


def test_parse_native_fetch_media(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    (tmp_path / "tooling.yaml").write_text(
        (tmp_path / "tooling.yaml").read_text(encoding="utf-8")
        + """
native:
  fetch_media:
    dk_env: prod
    dest: var/media
    legacy:
      remote: /backup/django_media
      ssh_host: legacy.example
""",
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    assert cfg.default_fetch_dk_env == "prod"
    assert cfg.fetch_media_dest_path == tmp_path / "var/media"
    assert cfg.native.fetch_media.legacy == FetchMediaLegacyConfig(
        remote="/backup/django_media",
        ssh_host="legacy.example",
        default=False,
    )


def test_parse_native_fetch_media_legacy_default_true(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    (tmp_path / "tooling.yaml").write_text(
        (tmp_path / "tooling.yaml").read_text(encoding="utf-8")
        + """
native:
  fetch_media:
    legacy:
      default: true
      remote: /backup/django_media
      ssh_host: legacy.example
""",
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    assert cfg.native.fetch_media.legacy == FetchMediaLegacyConfig(
        remote="/backup/django_media",
        ssh_host="legacy.example",
        default=True,
    )


def test_native_parser_fetch_media_legacy_path_default(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    (tmp_path / "tooling.yaml").write_text(
        (tmp_path / "tooling.yaml").read_text(encoding="utf-8")
        + """
native:
  fetch_media:
    legacy:
      default: true
      remote: /backup/django_media
      ssh_host: legacy.example
""",
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    parser, _ = build_native_parser(cfg)
    ns = parser.parse_args(["fetch", "media"])
    assert ns.legacy_path is True
    ns_off = parser.parse_args(["fetch", "media", "--no-legacy-path"])
    assert ns_off.legacy_path is False


def test_dk_info_fetch_media_defaults(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    info = {
        "docker_host": "ssh://deploy@host.example",
        "env": {"compose_project_name": "pas_indmo_prod"},
    }
    (env_dir / "info.yaml").write_text(yaml.dump(info), encoding="utf-8")
    cfg = load_project_config(tmp_path)
    ssh, project = dk_info_fetch_media_defaults(cfg, "prod")
    assert ssh == "deploy@host.example"
    assert project == "pas_indmo_prod"


def test_ssh_target_from_host() -> None:
    assert ssh_target_from_host("host.example") == "root@host.example"
    assert ssh_target_from_host("user@host.example") == "user@host.example"


def test_docker_volume_mountpoint_parses_json(monkeypatch) -> None:
    payload = '[{"Mountpoint": "/var/lib/docker/volumes/vol/_data"}]'

    def fake_run(cmd, **kwargs):
        class Result:
            returncode = 0
            stdout = payload
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)
    assert docker_volume_mountpoint_ssh("root@h", "vol") == "/var/lib/docker/volumes/vol/_data"


def test_run_fetch_media_docker_volume(tmp_path: Path, isolated_tooling: None, monkeypatch) -> None:
    _write_minimal_tooling(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        yaml.dump({"docker_host": "ssh://u@h", "env": {}}),
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    calls: list[tuple[str, str, Path]] = []

    def fake_rsync(ssh_target: str, remote_path: str, local_path: Path) -> int:
        calls.append((ssh_target, remote_path, local_path))
        return 0

    monkeypatch.setattr(
        "catalpa_tooling.fetch_media.try_docker_volume_mountpoint_ssh",
        lambda _s, _v, **_: "/vol/mount",
    )
    monkeypatch.setattr("catalpa_tooling.fetch_media.rsync_pull_remote_to_local", fake_rsync)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(
        "catalpa_tooling.fetch_media.ensure_ssh_known_host_for_ssh_target",
        lambda *_a, **_k: 0,
    )

    run_fetch_media(cfg, dk_env="prod", host=None, dest=tmp_path / "media", partial=False, legacy_path=False, legacy_remote=None, compose_project=None)
    assert calls == [("u@h", "/vol/mount/", tmp_path / "media")]


def test_run_fetch_media_falls_back_to_storage_path(
    tmp_path: Path, isolated_tooling: None, monkeypatch
) -> None:
    _write_minimal_tooling(tmp_path)
    env_dir = tmp_path / "docker" / "envs" / "prod"
    env_dir.mkdir(parents=True)
    (env_dir / "info.yaml").write_text(
        yaml.dump(
            {
                "docker_host": "ssh://u@h",
                "env": {},
                "storage": {"volumes": {"django_media": {"path": "/mnt/jid-media"}}},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_project_config(tmp_path)
    calls: list[tuple[str, str, Path]] = []

    def fake_rsync(ssh_target: str, remote_path: str, local_path: Path) -> int:
        calls.append((ssh_target, remote_path, local_path))
        return 0

    monkeypatch.setattr(
        "catalpa_tooling.fetch_media.try_docker_volume_mountpoint_ssh",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr("catalpa_tooling.fetch_media.rsync_pull_remote_to_local", fake_rsync)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/x")
    monkeypatch.setattr(
        "catalpa_tooling.fetch_media.ensure_ssh_known_host_for_ssh_target",
        lambda *_a, **_k: 0,
    )

    run_fetch_media(
        cfg,
        dk_env="prod",
        host=None,
        dest=tmp_path / "media",
        partial=False,
        legacy_path=False,
        legacy_remote=None,
        compose_project=None,
    )
    assert calls == [("u@h", "/mnt/jid-media/", tmp_path / "media")]


def test_run_fetch_media_legacy_requires_host(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    cfg = load_project_config(tmp_path)
    with pytest.raises(ValueError, match="legacy"):
        run_fetch_media(
            cfg,
            dk_env="prod",
            host=None,
            dest=None,
            partial=False,
            legacy_path=True,
            legacy_remote="/backup/media",
            compose_project=None,
        )


def test_legacy_remote_must_be_mapping(tmp_path: Path, isolated_tooling: None) -> None:
    _write_minimal_tooling(tmp_path)
    text = (tmp_path / "tooling.yaml").read_text(encoding="utf-8") + "\ndev:\n  fetch_media:\n    legacy: bad\n"
    (tmp_path / "tooling.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(ProjectConfigError, match="legacy"):
        load_project_config(tmp_path)
