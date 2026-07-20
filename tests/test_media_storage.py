"""Tests for media storage resolution (bind vs named volume)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from catalpa_tooling.media_storage import (
    MediaStorageError,
    MediaStorageKind,
    find_media_mount_in_compose,
    find_media_mount_in_config,
    resolve_media_storage,
)


def test_find_media_mount_short_bind(tmp_path: Path) -> None:
    compose = tmp_path / "compose.dev.yml"
    compose.write_text(
        "services:\n"
        "  django:\n"
        "    volumes:\n"
        "      - ./media:/media\n"
        "  caddy:\n"
        "    volumes:\n"
        "      - ./media:/srv/media:ro\n",
        encoding="utf-8",
    )
    assert find_media_mount_in_compose(compose) == ("bind", "./media", "/media")


def test_find_media_mount_django_media_target(tmp_path: Path) -> None:
    compose = tmp_path / "compose.dev.yml"
    compose.write_text(
        "services:\n"
        "  django:\n"
        "    volumes:\n"
        "      - ./media:/django_media\n",
        encoding="utf-8",
    )
    assert find_media_mount_in_compose(compose) == ("bind", "./media", "/django_media")


def test_find_media_mount_short_volume(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  django:\n"
        "    volumes:\n"
        "      - django_media:/media\n",
        encoding="utf-8",
    )
    assert find_media_mount_in_compose(compose) == ("volume", "django_media", "/media")


def test_find_media_mount_volume_key_even_on_odd_target(tmp_path: Path) -> None:
    """ops.restic.data_volume key wins even if target is unusual."""
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  django:\n"
        "    volumes:\n"
        "      - django_media:/custom/media/root\n",
        encoding="utf-8",
    )
    assert find_media_mount_in_compose(compose) == (
        "volume",
        "django_media",
        "/custom/media/root",
    )


def test_find_media_mount_long_syntax(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  web:\n"
        "    volumes:\n"
        "      - type: bind\n"
        "        source: ./var/media\n"
        "        target: /media\n",
        encoding="utf-8",
    )
    assert find_media_mount_in_compose(compose) == ("bind", "./var/media", "/media")


def test_find_media_mount_in_config_prefers_django_media_root() -> None:
    data = {
        "services": {
            "django": {
                "environment": {"DJANGO_MEDIA_ROOT": "/data/uploads"},
                "volumes": [
                    {
                        "type": "bind",
                        "source": "/host/uploads",
                        "target": "/data/uploads",
                    },
                    {"type": "volume", "source": "django_static", "target": "/static"},
                ],
            }
        }
    }
    assert find_media_mount_in_config(data) == ("bind", "/host/uploads", "/data/uploads")


def test_find_media_mount_in_config_ignores_srv_media() -> None:
    data = {
        "services": {
            "caddy": {
                "volumes": [
                    {"type": "bind", "source": "/host/m", "target": "/srv/media"},
                ]
            },
            "django": {
                "volumes": [
                    {"type": "volume", "source": "django_media", "target": "/django_media"},
                ]
            },
        }
    }
    assert find_media_mount_in_config(data) == ("volume", "django_media", "/django_media")


def test_resolve_media_storage_bind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimal_project
) -> None:
    compose = tmp_path / "compose.dev.yml"
    compose.write_text(
        "services:\n  django:\n    volumes:\n      - ./media:/media\n",
        encoding="utf-8",
    )
    (tmp_path / "media").mkdir()
    # Force YAML fallback so the test does not depend on docker compose succeeding.
    monkeypatch.setattr(
        "catalpa_tooling.media_storage.load_compose_config",
        lambda *a, **k: None,
    )
    storage = resolve_media_storage(
        compose_file=str(compose),
        env={"COMPOSE_PROJECT_NAME": "app_dev"},
        config=minimal_project,
        repo_root=tmp_path,
    )
    assert storage.kind is MediaStorageKind.BIND
    assert storage.location == str((tmp_path / "media").resolve())


def test_resolve_media_storage_via_compose_config_bind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimal_project
) -> None:
    compose = tmp_path / "compose.dev.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(
        "catalpa_tooling.media_storage.load_compose_config",
        lambda *a, **k: {
            "services": {
                "django": {
                    "environment": {"DJANGO_MEDIA_ROOT": "/django_media"},
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(media.resolve()),
                            "target": "/django_media",
                        }
                    ],
                }
            },
            "volumes": {},
        },
    )
    storage = resolve_media_storage(
        compose_file=str(compose),
        env={"COMPOSE_PROJECT_NAME": "app_dev"},
        config=minimal_project,
        repo_root=tmp_path,
    )
    assert storage.kind is MediaStorageKind.BIND
    assert storage.location == str(media.resolve())


def test_resolve_media_storage_via_compose_config_volume_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimal_project
) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "catalpa_tooling.media_storage.load_compose_config",
        lambda *a, **k: {
            "services": {
                "django": {
                    "volumes": [
                        {
                            "type": "volume",
                            "source": "django_media",
                            "target": "/django_media",
                        }
                    ],
                }
            },
            "volumes": {
                "django_media": {"name": "app_full_django_media"},
            },
        },
    )
    storage = resolve_media_storage(
        compose_file=str(compose),
        env={"COMPOSE_PROJECT_NAME": "app_full"},
        config=minimal_project,
        repo_root=tmp_path,
    )
    assert storage.kind is MediaStorageKind.VOLUME
    assert storage.location == "app_full_django_media"


def test_resolve_media_storage_volume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimal_project
) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n  django:\n    volumes:\n      - django_media:/media\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "catalpa_tooling.media_storage.load_compose_config",
        lambda *a, **k: None,
    )
    storage = resolve_media_storage(
        compose_file=str(compose),
        env={"COMPOSE_PROJECT_NAME": "app_full"},
        config=minimal_project,
        repo_root=tmp_path,
    )
    assert storage.kind is MediaStorageKind.VOLUME
    assert storage.location.endswith("_django_media")
    assert "app_full" in storage.location


def test_resolve_media_storage_fallback_no_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimal_project
) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  django:\n    image: x\n", encoding="utf-8")
    monkeypatch.setattr(
        "catalpa_tooling.media_storage.load_compose_config",
        lambda *a, **k: None,
    )
    storage = resolve_media_storage(
        compose_file=str(compose),
        env={"COMPOSE_PROJECT_NAME": "app"},
        config=minimal_project,
        repo_root=tmp_path,
    )
    assert storage.kind is MediaStorageKind.VOLUME
    assert storage.location == "app_django_media"


def test_resolve_media_storage_bind_rejects_remote_docker_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, minimal_project
) -> None:
    compose = tmp_path / "compose.dev.yml"
    compose.write_text(
        "services:\n  django:\n    volumes:\n      - ./media:/media\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "catalpa_tooling.media_storage.load_compose_config",
        lambda *a, **k: None,
    )
    with pytest.raises(MediaStorageError, match="remote"):
        resolve_media_storage(
            compose_file=str(compose),
            env={
                "COMPOSE_PROJECT_NAME": "app_dev",
                "DOCKER_HOST": "ssh://user@host",
            },
            config=minimal_project,
            repo_root=tmp_path,
        )


def test_prefer_web_service_over_other(tmp_path: Path, minimal_project) -> None:
    compose = tmp_path / "compose.yml"
    web_name = minimal_project.stack.services.web
    compose.write_text(
        "services:\n"
        f"  other:\n"
        "    volumes:\n"
        "      - ./other_media:/media\n"
        f"  {web_name}:\n"
        "    volumes:\n"
        "      - ./web_media:/media\n",
        encoding="utf-8",
    )
    mount = find_media_mount_in_compose(compose, config=minimal_project)
    assert mount == ("bind", "./web_media", "/media")


def test_load_compose_config_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from catalpa_tooling.media_storage import load_compose_config

    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    m.stderr = "boom"
    monkeypatch.setattr("catalpa_tooling.media_storage.run_cmd", lambda *a, **k: m)
    assert load_compose_config(str(tmp_path / "c.yml"), {}) is None
