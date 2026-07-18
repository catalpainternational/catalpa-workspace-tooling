"""Tests for media storage resolution (bind vs named volume)."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalpa_tooling.media_storage import (
    MediaStorageError,
    MediaStorageKind,
    find_media_mount_in_compose,
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


def test_resolve_media_storage_bind(tmp_path: Path, minimal_project) -> None:
    compose = tmp_path / "compose.dev.yml"
    compose.write_text(
        "services:\n  django:\n    volumes:\n      - ./media:/media\n",
        encoding="utf-8",
    )
    (tmp_path / "media").mkdir()
    storage = resolve_media_storage(
        compose_file=str(compose),
        env={"COMPOSE_PROJECT_NAME": "app_dev"},
        config=minimal_project,
        repo_root=tmp_path,
    )
    assert storage.kind is MediaStorageKind.BIND
    assert storage.location == str((tmp_path / "media").resolve())


def test_resolve_media_storage_volume(tmp_path: Path, minimal_project) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n  django:\n    volumes:\n      - django_media:/media\n",
        encoding="utf-8",
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


def test_resolve_media_storage_fallback_no_mount(tmp_path: Path, minimal_project) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text("services:\n  django:\n    image: x\n", encoding="utf-8")
    storage = resolve_media_storage(
        compose_file=str(compose),
        env={"COMPOSE_PROJECT_NAME": "app"},
        config=minimal_project,
        repo_root=tmp_path,
    )
    assert storage.kind is MediaStorageKind.VOLUME
    assert storage.location == "app_django_media"


def test_resolve_media_storage_bind_rejects_remote_docker_host(
    tmp_path: Path, minimal_project
) -> None:
    compose = tmp_path / "compose.dev.yml"
    compose.write_text(
        "services:\n  django:\n    volumes:\n      - ./media:/media\n",
        encoding="utf-8",
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
