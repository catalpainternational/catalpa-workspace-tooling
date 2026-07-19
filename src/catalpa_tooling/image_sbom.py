"""Generate and attach CycloneDX SBOMs for stack images during ``dk push``."""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
from pathlib import Path

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.run_cmd import run as run_cmd

# Pin tool images; bump intentionally when upgrading scanners/attach tooling.
SYFT_IMAGE = "anchore/syft:v1.46.0"
ORAS_IMAGE = "ghcr.io/oras-project/oras:v1.2.2"

CYCLONEDX_MEDIA_TYPE = "application/vnd.cyclonedx+json"
ORAS_ARTIFACT_TYPE = "application/vnd.cyclonedx+json"

_APP_MERGE_ROLES = frozenset({"web", "proxy"})


def should_merge_app_bom(role: str) -> bool:
    """Whether to union the compliance app SBOM into this stack role's image SBOM."""
    return role in _APP_MERGE_ROLES


def app_bom_path(config: ProjectConfig) -> Path | None:
    """Committed merged app SBOM path when ``compliance:`` is configured."""
    compliance = config.compliance
    if compliance is None:
        return None
    return config.repo_root / compliance.outputs.sbom_dir / "bom.cdx.json"


def merge_cyclonedx(image_bom: dict, app_bom: dict) -> dict:
    """Union CycloneDX components; prefer image metadata with a stack-wide note."""
    components: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(block: object) -> None:
        if not isinstance(block, list):
            return
        for item in block:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("type") or ""),
                str(item.get("name") or ""),
                str(item.get("version") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            components.append(item)

    _add(image_bom.get("components"))
    _add(app_bom.get("components"))
    components.sort(key=lambda c: (c.get("name") or "", c.get("version") or "", c.get("type") or ""))

    metadata = dict(image_bom.get("metadata") or {}) if isinstance(image_bom.get("metadata"), dict) else {}
    props = list(metadata.get("properties") or []) if isinstance(metadata.get("properties"), list) else []
    props.append(
        {
            "name": "catalpa.sbom.merge",
            "value": "image-scan+stack-app-inventory",
        }
    )
    metadata["properties"] = props

    return {
        "bomFormat": "CycloneDX",
        "specVersion": image_bom.get("specVersion") or app_bom.get("specVersion") or "1.5",
        "version": 1,
        "metadata": metadata,
        "components": components,
    }


def _which(name: str) -> str | None:
    return shutil.which(name)


def _docker_config_dir() -> Path:
    override = (os.environ.get("DOCKER_CONFIG") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".docker"


def registry_host_from_ref(image_ref: str) -> str:
    """Registry hostname for an image ref (``ghcr.io/…`` → ``ghcr.io``)."""
    name = image_ref.split("@", 1)[0]
    if "/" not in name:
        return "docker.io"
    first = name.split("/", 1)[0]
    if "." in first or ":" in first or first == "localhost":
        return first
    return "docker.io"


def _cred_helper_binary(helper: str) -> str:
    if helper.startswith("docker-credential-"):
        return helper
    return f"docker-credential-{helper}"


def _cred_helper_get(helper: str, server: str) -> tuple[str, str] | None:
    """Query a Docker credential helper; return (username, secret) or None."""
    exe = _which(_cred_helper_binary(helper))
    if not exe:
        return None
    result = run_cmd(
        [exe, "get"],
        input=f"{server}\n",
        check=False,
        capture_output=True,
        text=True,
        print_cmd=False,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads((result.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    user = str(payload.get("Username") or "").strip()
    secret = str(payload.get("Secret") or "").strip()
    if not user or not secret:
        return None
    return user, secret


def _auth_from_auths_entry(entry: object) -> tuple[str, str] | None:
    if not isinstance(entry, dict):
        return None
    auth = entry.get("auth")
    if isinstance(auth, str) and auth.strip():
        try:
            decoded = base64.b64decode(auth.strip()).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        if ":" not in decoded:
            return None
        user, _, secret = decoded.partition(":")
        if user and secret:
            return user, secret
    user = str(entry.get("username") or "").strip()
    secret = str(entry.get("password") or entry.get("identitytoken") or "").strip()
    if user and secret:
        return user, secret
    return None


def lookup_registry_credentials(registry_host: str) -> tuple[str, str] | None:
    """Resolve registry username/password from the host Docker config / cred helpers.

    Used so dockerized ORAS does not depend on host-only helpers (e.g. osxkeychain).
    """
    cfg_path = _docker_config_dir() / "config.json"
    data = _load_json(cfg_path) or {}
    host = registry_host.strip().rstrip("/")
    server_candidates = [host, f"https://{host}", f"http://{host}", f"https://{host}/v2/"]

    auths = data.get("auths") if isinstance(data.get("auths"), dict) else {}
    for server in server_candidates:
        creds = _auth_from_auths_entry(auths.get(server))
        if creds:
            return creds

    helpers = data.get("credHelpers") if isinstance(data.get("credHelpers"), dict) else {}
    for server in server_candidates:
        helper = helpers.get(server) or helpers.get(host)
        if isinstance(helper, str) and helper.strip():
            creds = _cred_helper_get(helper.strip(), server)
            if creds:
                return creds

    store = data.get("credsStore")
    if isinstance(store, str) and store.strip():
        for server in server_candidates:
            creds = _cred_helper_get(store.strip(), server)
            if creds:
                return creds
    return None


def write_inline_docker_auth_config(target_dir: Path, registry_host: str, username: str, secret: str) -> Path:
    """Write a Docker config.json with inline auth (no credential helpers)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    token = base64.b64encode(f"{username}:{secret}".encode("utf-8")).decode("ascii")
    host = registry_host.strip().rstrip("/")
    payload = {
        "auths": {
            host: {"auth": token},
            f"https://{host}": {"auth": token},
        }
    }
    cfg = target_dir / "config.json"
    cfg.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return cfg


def generate_image_cyclonedx(image_ref: str, output_path: Path) -> int:
    """Run Syft against a local/registry image ref; write CycloneDX JSON to ``output_path``."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    host_syft = _which("syft")
    if host_syft:
        result = run_cmd(
            [host_syft, image_ref, "-o", f"cyclonedx-json={output_path}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            print(f"syft failed for {image_ref}: {detail or 'non-zero exit'}", flush=True)
        return result.returncode

    out_dir = output_path.parent.resolve()
    out_name = output_path.name
    result = run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            "/var/run/docker.sock:/var/run/docker.sock",
            "-v",
            f"{out_dir}:/sbom-out",
            SYFT_IMAGE,
            image_ref,
            "-o",
            f"cyclonedx-json=/sbom-out/{out_name}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        print(f"syft (docker) failed for {image_ref}: {detail or 'non-zero exit'}", flush=True)
    return result.returncode


_SKIP_SBOM_HINT = "To skip image SBOMs: `uv run dk push --no-sbom`"


def attach_sbom_referrer(image_ref_with_digest: str, sbom_path: Path) -> int:
    """Attach a CycloneDX file as an OCI referrer to ``image@sha256:…`` via ORAS."""
    if not sbom_path.is_file():
        print(f"SBOM file missing for attach: {sbom_path}", flush=True)
        print(_SKIP_SBOM_HINT, flush=True)
        return 1
    work_dir = sbom_path.parent.resolve()
    # ORAS rejects absolute file paths; pass a relative name with cwd / -w set.
    file_arg = f"{sbom_path.name}:{CYCLONEDX_MEDIA_TYPE}"
    host_oras = _which("oras")
    if host_oras:
        result = run_cmd(
            [
                host_oras,
                "attach",
                "--artifact-type",
                ORAS_ARTIFACT_TYPE,
                image_ref_with_digest,
                file_arg,
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(work_dir),
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            print(
                f"oras attach failed for {image_ref_with_digest}: {detail or 'non-zero exit'}",
                flush=True,
            )
            print(_SKIP_SBOM_HINT, flush=True)
        return result.returncode

    registry_host = registry_host_from_ref(image_ref_with_digest)
    with tempfile.TemporaryDirectory(prefix="dk-oras-docker-cfg-") as auth_tmp:
        auth_dir = Path(auth_tmp)
        creds = lookup_registry_credentials(registry_host)
        if creds:
            write_inline_docker_auth_config(auth_dir, registry_host, creds[0], creds[1])
        elif (_docker_config_dir() / "config.json").is_file():
            # Last resort: copy config as-is (works when auths are already inline).
            shutil.copy2(_docker_config_dir() / "config.json", auth_dir / "config.json")
        else:
            print(
                f"No Docker credentials found for {registry_host}; "
                "oras attach may fail (docker login first)",
                flush=True,
            )

        cmd = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{work_dir}:/workspace",
            "-w",
            "/workspace",
            "-v",
            f"{auth_dir}:/root/.docker:ro",
            "-e",
            "DOCKER_CONFIG=/root/.docker",
            ORAS_IMAGE,
            "attach",
            "--artifact-type",
            ORAS_ARTIFACT_TYPE,
            image_ref_with_digest,
            file_arg,
        ]
        result = run_cmd(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            print(
                f"oras (docker) attach failed for {image_ref_with_digest}: "
                f"{detail or 'non-zero exit'}",
                flush=True,
            )
            print(_SKIP_SBOM_HINT, flush=True)
        return result.returncode


def resolve_repo_digest(image_ref: str, registry: str) -> str | None:
    """Return ``name@sha256:…`` for ``image_ref`` matching ``registry`` after push."""
    result = run_cmd(
        ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image_ref],
        check=False,
        capture_output=True,
        text=True,
        print_cmd=False,
    )
    if result.returncode != 0:
        return None
    try:
        digests = json.loads((result.stdout or "").strip() or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(digests, list):
        return None
    reg = registry.rstrip("/")
    # Prefer digest under the same registry; fall back to first entry.
    preferred: str | None = None
    for entry in digests:
        text = str(entry).strip()
        if not text or "@" not in text:
            continue
        if text.startswith(reg + "/") or text.startswith(reg):
            return text
        if preferred is None:
            preferred = text
    if preferred:
        return preferred
    # Build name@digest from tag ref + Id if RepoDigests empty (unusual after push).
    result_id = run_cmd(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image_ref],
        check=False,
        capture_output=True,
        text=True,
        print_cmd=False,
    )
    image_id = (result_id.stdout or "").strip()
    if result_id.returncode == 0 and image_id.startswith("sha256:"):
        name = image_ref.rsplit(":", 1)[0]
        return f"{name}@{image_id}"
    return None


def _load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def prepare_and_attach_image_sbom(
    config: ProjectConfig,
    *,
    role: str,
    image_ref: str,
    registry: str,
) -> int:
    """Syft scan, optionally merge app bom, attach ORAS referrer to the pushed digest."""
    digest_ref = resolve_repo_digest(image_ref, registry)
    if not digest_ref:
        print(f"Could not resolve RepoDigest for {image_ref} after push", flush=True)
        return 1

    with tempfile.TemporaryDirectory(prefix="dk-image-sbom-") as tmp:
        tmp_path = Path(tmp)
        syft_path = tmp_path / "syft.cdx.json"
        syft_rc = generate_image_cyclonedx(image_ref, syft_path)
        if syft_rc != 0:
            return syft_rc
        image_bom = _load_json(syft_path)
        if image_bom is None:
            print(f"Syft produced unreadable SBOM for {image_ref}", flush=True)
            return 1

        attach_path = syft_path
        if should_merge_app_bom(role):
            bom_file = app_bom_path(config)
            if bom_file is not None:
                if not bom_file.is_file():
                    print(
                        f"Compliance app SBOM missing ({bom_file}); "
                        "run `uv run tests compliance` before `dk push`, "
                        "or skip with `uv run dk push --no-sbom`",
                        flush=True,
                    )
                    return 1
                app_bom = _load_json(bom_file)
                if app_bom is None:
                    print(
                        f"Could not parse app SBOM {bom_file}. {_SKIP_SBOM_HINT}",
                        flush=True,
                    )
                    return 1
                merged = merge_cyclonedx(image_bom, app_bom)
                attach_path = tmp_path / "merged.cdx.json"
                attach_path.write_text(
                    json.dumps(merged, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

        print(f"Attaching CycloneDX SBOM to {digest_ref} (role={role})", flush=True)
        return attach_sbom_referrer(digest_ref, attach_path)
