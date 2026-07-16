"""Unit tests for image SBOM generation and ``dk push`` attach wiring."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from catalpa_tooling.build_push import push_images
from catalpa_tooling.config import (
    ComplianceConfig,
    ComplianceOutputsConfig,
    load_project_config,
)
from catalpa_tooling.dk_parser import build_dk_parser
from catalpa_tooling.dk_stack import push_registry_images
from catalpa_tooling.image_sbom import (
    ORAS_IMAGE,
    merge_cyclonedx,
    prepare_and_attach_image_sbom,
    should_merge_app_bom,
)
from tests.helpers import write_minimal_tooling_tree


def _compliance(*, sbom_dir: str = "compliance/sbom") -> ComplianceConfig:
    return ComplianceConfig(
        project_license="LicenseRef-pending",
        license_files=("LICENSE",),
        python=None,
        javascript=None,
        bundled_assets=(),
        forbidden_spdx=(),
        warn_spdx=(),
        allow_strong_copyleft=False,
        outputs=ComplianceOutputsConfig(sbom_dir=sbom_dir, notices="compliance/THIRD_PARTY_NOTICES.md"),
    )


def test_should_merge_app_bom_roles() -> None:
    assert should_merge_app_bom("web") is True
    assert should_merge_app_bom("proxy") is True
    assert should_merge_app_bom("db") is False


def test_merge_cyclonedx_unions_and_dedupes() -> None:
    image = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "components": [
            {"type": "library", "name": "pgbackrest", "version": "2.50"},
            {"type": "library", "name": "shared", "version": "1.0"},
        ],
        "metadata": {},
    }
    app = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "components": [
            {"type": "library", "name": "django", "version": "5.0"},
            {"type": "library", "name": "shared", "version": "1.0"},
        ],
    }
    merged = merge_cyclonedx(image, app)
    names = {(c["name"], c["version"]) for c in merged["components"]}
    assert names == {("pgbackrest", "2.50"), ("shared", "1.0"), ("django", "5.0")}
    props = merged["metadata"]["properties"]
    assert any(p.get("name") == "catalpa.sbom.merge" for p in props)


def test_dk_parser_no_sbom_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_minimal_tooling_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    config = load_project_config(tmp_path)
    parser = build_dk_parser(config)
    ns = parser.parse_args(["push", "--no-sbom", "--tag", "v1"])
    assert ns.dk_command == "push"
    assert ns.no_sbom is True
    assert ns.tag == "v1"


def test_push_registry_images_skips_sbom_when_disabled(
    minimal_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("catalpa_tooling.dk_stack.run_cmd", fake_run)

    import catalpa_tooling.image_sbom as image_sbom_mod

    attach = MagicMock(return_value=0)
    monkeypatch.setattr(image_sbom_mod, "prepare_and_attach_image_sbom", attach)

    rc = push_registry_images(minimal_project, "ghcr.io/example/app", "v1", sbom=False)
    assert rc == 0
    assert attach.call_count == 0
    assert sum(1 for c in calls if c[:2] == ["docker", "push"]) == 3


def test_push_registry_images_attaches_per_role(
    minimal_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("catalpa_tooling.dk_stack.run_cmd", fake_run)
    attached: list[tuple[str, str]] = []

    def fake_attach(config, *, role, image_ref, registry):
        attached.append((role, image_ref))
        return 0

    import catalpa_tooling.image_sbom as image_sbom_mod

    monkeypatch.setattr(image_sbom_mod, "prepare_and_attach_image_sbom", fake_attach)

    rc = push_registry_images(minimal_project, "ghcr.io/example/app", "v9", sbom=True)
    assert rc == 0
    assert [r for r, _ in attached] == ["web", "proxy", "db"]
    assert all(ref.endswith(":v9") for _, ref in attached)


def test_prepare_web_merges_app_bom(
    minimal_project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = replace(minimal_project, compliance=_compliance())
    bom_dir = config.repo_root / "compliance" / "sbom"
    bom_dir.mkdir(parents=True)
    (bom_dir / "bom.cdx.json").write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.5",
                "components": [{"type": "library", "name": "django", "version": "5.0"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "catalpa_tooling.image_sbom.resolve_repo_digest",
        lambda image_ref, registry: f"{image_ref}@sha256:abc",
    )

    def fake_syft(image_ref: str, output_path: Path) -> int:
        output_path.write_text(
            json.dumps(
                {
                    "bomFormat": "CycloneDX",
                    "specVersion": "1.5",
                    "metadata": {},
                    "components": [{"type": "library", "name": "bash", "version": "5"}],
                }
            ),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("catalpa_tooling.image_sbom.generate_image_cyclonedx", fake_syft)

    attached: list[Path] = []

    def fake_oras(digest_ref: str, sbom_path: Path) -> int:
        attached.append(sbom_path)
        data = json.loads(sbom_path.read_text(encoding="utf-8"))
        names = {c["name"] for c in data["components"]}
        assert names == {"bash", "django"}
        return 0

    monkeypatch.setattr("catalpa_tooling.image_sbom.attach_sbom_referrer", fake_oras)

    rc = prepare_and_attach_image_sbom(
        config,
        role="web",
        image_ref="ghcr.io/example/app/app-web:v1",
        registry="ghcr.io/example/app",
    )
    assert rc == 0
    assert len(attached) == 1


def test_prepare_db_skips_app_bom(
    minimal_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(minimal_project, compliance=_compliance())
    bom_dir = config.repo_root / "compliance" / "sbom"
    bom_dir.mkdir(parents=True)
    (bom_dir / "bom.cdx.json").write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "components": [{"type": "library", "name": "django", "version": "5.0"}],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "catalpa_tooling.image_sbom.resolve_repo_digest",
        lambda image_ref, registry: f"{image_ref}@sha256:db",
    )

    def fake_syft(image_ref: str, output_path: Path) -> int:
        output_path.write_text(
            json.dumps(
                {
                    "bomFormat": "CycloneDX",
                    "metadata": {},
                    "components": [{"type": "library", "name": "pgbackrest", "version": "2.50"}],
                }
            ),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("catalpa_tooling.image_sbom.generate_image_cyclonedx", fake_syft)

    def fake_oras(digest_ref: str, sbom_path: Path) -> int:
        data = json.loads(sbom_path.read_text(encoding="utf-8"))
        names = {c["name"] for c in data["components"]}
        assert names == {"pgbackrest"}
        assert "django" not in names
        return 0

    monkeypatch.setattr("catalpa_tooling.image_sbom.attach_sbom_referrer", fake_oras)

    rc = prepare_and_attach_image_sbom(
        config,
        role="db",
        image_ref="ghcr.io/example/app/app-db:v1",
        registry="ghcr.io/example/app",
    )
    assert rc == 0


def test_prepare_web_fails_when_app_bom_missing(
    minimal_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace(minimal_project, compliance=_compliance())
    monkeypatch.setattr(
        "catalpa_tooling.image_sbom.resolve_repo_digest",
        lambda image_ref, registry: f"{image_ref}@sha256:abc",
    )

    def fake_syft(image_ref: str, output_path: Path) -> int:
        output_path.write_text(
            json.dumps({"bomFormat": "CycloneDX", "metadata": {}, "components": []}),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("catalpa_tooling.image_sbom.generate_image_cyclonedx", fake_syft)
    monkeypatch.setattr(
        "catalpa_tooling.image_sbom.attach_sbom_referrer",
        lambda *a, **k: 0,
    )

    rc = prepare_and_attach_image_sbom(
        config,
        role="web",
        image_ref="ghcr.io/example/app/app-web:v1",
        registry="ghcr.io/example/app",
    )
    assert rc == 1


def test_prepare_fails_when_syft_fails(
    minimal_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.image_sbom.resolve_repo_digest",
        lambda image_ref, registry: f"{image_ref}@sha256:abc",
    )
    monkeypatch.setattr(
        "catalpa_tooling.image_sbom.generate_image_cyclonedx",
        lambda *a, **k: 2,
    )
    rc = prepare_and_attach_image_sbom(
        minimal_project,
        role="db",
        image_ref="ghcr.io/example/app/app-db:v1",
        registry="ghcr.io/example/app",
    )
    assert rc == 2


def test_push_images_no_sbom_wires_flag(
    minimal_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "catalpa_tooling.build_push._load_images_config",
        lambda config: {"image_registry": "ghcr.io/example/app", "image_tag": "latest"},
    )
    monkeypatch.setattr("catalpa_tooling.build_push.compose_yml_build", lambda *a, **k: 0)
    monkeypatch.setattr("catalpa_tooling.build_push.tag_local_to_registry", lambda *a, **k: 0)

    seen: dict[str, bool] = {}

    def fake_push(config, registry, tag, *, sbom=True):
        seen["sbom"] = sbom
        return 0

    monkeypatch.setattr("catalpa_tooling.build_push.push_registry_images", fake_push)
    assert push_images(minimal_project, sbom=False) == 0
    assert seen["sbom"] is False


def test_registry_host_from_ref() -> None:
    from catalpa_tooling.image_sbom import registry_host_from_ref

    assert (
        registry_host_from_ref(
            "ghcr.io/catalpainternational/catalpa_bero-django@sha256:abc"
        )
        == "ghcr.io"
    )
    assert registry_host_from_ref("nginx:latest") == "docker.io"


def test_write_inline_docker_auth_config(tmp_path: Path) -> None:
    from catalpa_tooling.image_sbom import write_inline_docker_auth_config

    cfg = write_inline_docker_auth_config(tmp_path, "ghcr.io", "user", "token")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "credsStore" not in data
    assert "ghcr.io" in data["auths"]
    decoded = base64.b64decode(data["auths"]["ghcr.io"]["auth"]).decode("utf-8")
    assert decoded == "user:token"


def test_lookup_registry_credentials_from_auths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling.image_sbom import lookup_registry_credentials

    cfg_dir = tmp_path / "docker"
    cfg_dir.mkdir()
    token = base64.b64encode(b"alice:s3cret").decode("ascii")
    (cfg_dir / "config.json").write_text(
        json.dumps({"auths": {"ghcr.io": {"auth": token}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(cfg_dir))
    assert lookup_registry_credentials("ghcr.io") == ("alice", "s3cret")


def test_lookup_registry_credentials_via_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling import image_sbom as mod

    cfg_dir = tmp_path / "docker"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"credsStore": "osxkeychain"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_CONFIG", str(cfg_dir))

    def fake_helper_get(helper: str, server: str):
        assert helper == "osxkeychain"
        assert "ghcr.io" in server
        return ("bob", "pat")

    monkeypatch.setattr(mod, "_cred_helper_get", fake_helper_get)
    assert mod.lookup_registry_credentials("ghcr.io") == ("bob", "pat")


def test_attach_sbom_docker_uses_inline_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalpa_tooling import image_sbom as mod

    sbom = tmp_path / "merged.cdx.json"
    sbom.write_text('{"bomFormat":"CycloneDX","components":[]}\n', encoding="utf-8")
    monkeypatch.setattr(mod, "_which", lambda name: None)
    monkeypatch.setattr(mod, "lookup_registry_credentials", lambda host: ("u", "p"))

    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod, "run_cmd", fake_run)
    rc = mod.attach_sbom_referrer(
        "ghcr.io/example/app@sha256:deadbeef",
        sbom,
    )
    assert rc == 0
    assert seen
    cmd = seen[0]
    assert "docker" in cmd[0]
    assert ORAS_IMAGE in cmd
    # Must mount a temp auth dir, not the real ~/.docker path blindly with keychain.
    assert any(a == "-v" and "/root/.docker:ro" in b for a, b in zip(cmd, cmd[1:]))
    assert "DOCKER_CONFIG=/root/.docker" in cmd
