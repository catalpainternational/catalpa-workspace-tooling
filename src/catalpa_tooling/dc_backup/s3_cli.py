"""Host-side ``garage-s3`` / ``garage-admin`` helpers installed by provision."""

from __future__ import annotations

import shlex
import sys

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dc_backup.ssh_install import install_files_via_ssh, remote_run
from catalpa_tooling.systemd_assets import systemd_source_dir
from catalpa_tooling.systemd_remote_install import redact_env_file_content

GARAGE_LOOPBACK_ENDPOINT = "http://127.0.0.1:3900"
ENV_FILENAME = "garage-s3.env"
S3_SCRIPT_NAME = "garage-s3"
ADMIN_SCRIPT_NAME = "garage-admin"
DEFAULT_AWS_CLI_IMAGE = "amazon/aws-cli:2.22.35"
# Usual login PATH includes this; ops.install_prefix often does not.
PATH_BIN_DIR = "/usr/local/bin"


def render_garage_s3_env(
    *,
    project_name: str,
    access_key_id: str,
    secret_access_key: str,
    bucket: str,
    region: str,
    admin_token: str = "",
    endpoint: str = GARAGE_LOOPBACK_ENDPOINT,
) -> str:
    """Render ``garage-s3.env`` for the backup host."""
    lines = [
        f"# Managed by {project_name} deploy (dk <env> dc-backup provision).",
        f"AWS_ACCESS_KEY_ID={access_key_id.strip()}",
        f"AWS_SECRET_ACCESS_KEY={secret_access_key.strip()}",
        f"AWS_DEFAULT_REGION={(region or 'garage').strip() or 'garage'}",
        f"AWS_ENDPOINT_URL={(endpoint or GARAGE_LOOPBACK_ENDPOINT).strip()}",
        f"GARAGE_BUCKET={(bucket or '').strip()}",
        f"AWS_CLI_IMAGE={DEFAULT_AWS_CLI_IMAGE}",
    ]
    token = (admin_token or "").strip()
    if token:
        lines.append(f"GARAGE_ADMIN_TOKEN={token}")
    return "\n".join(lines) + "\n"


def _render_s3_script(*, config_dir: str) -> str:
    src = systemd_source_dir() / S3_SCRIPT_NAME
    if not src.is_file():
        raise FileNotFoundError(f"Missing bundled script: {src}")
    return src.read_text(encoding="utf-8").replace("@CONFIG_DIR@", config_dir.rstrip("/"))


def _render_admin_script() -> str:
    src = systemd_source_dir() / ADMIN_SCRIPT_NAME
    if not src.is_file():
        raise FileNotFoundError(f"Missing bundled script: {src}")
    return src.read_text(encoding="utf-8")


def install_garage_s3_cli(
    ssh: str,
    config: ProjectConfig,
    *,
    access_key_id: str,
    secret_access_key: str,
    bucket: str,
    region: str,
    admin_token: str = "",
    dry_run: bool = False,
) -> None:
    """Install ``garage-s3``, ``garage-admin``, and env on the backup host.

    Failures are logged and non-fatal (caller should keep provision success).
    """
    install_prefix = (config.ops.install_prefix or "").strip().rstrip("/")
    config_dir = (config.ops.config_dir or "").strip().rstrip("/")
    if not install_prefix or not config_dir:
        print(
            "warning: ops.install_prefix and ops.config_dir are required to install "
            "garage-s3; skipping host CLI.",
            file=sys.stderr,
        )
        return

    try:
        env_body = render_garage_s3_env(
            project_name=config.meta.name,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            bucket=bucket,
            region=region,
            admin_token=admin_token,
            endpoint=GARAGE_LOOPBACK_ENDPOINT,
        )
        s3_script = _render_s3_script(config_dir=config_dir)
        admin_script = _render_admin_script()
    except FileNotFoundError as e:
        print(f"warning: {e}; skipping garage-s3 install.", file=sys.stderr)
        return

    s3_path = f"{install_prefix}/{S3_SCRIPT_NAME}"
    admin_path = f"{install_prefix}/{ADMIN_SCRIPT_NAME}"

    print(
        f"Installing garage-s3 CLI on {ssh} "
        f"({s3_path}, {admin_path}, {config_dir}/{ENV_FILENAME}; "
        f"symlinks in {PATH_BIN_DIR}/)",
        flush=True,
    )

    if dry_run:
        print(f"[dry-run] --- {config_dir}/{ENV_FILENAME} (redacted) ---", flush=True)
        print(redact_env_file_content(env_body), end="", flush=True)
        print(
            f"[dry-run] would install {S3_SCRIPT_NAME} + {ADMIN_SCRIPT_NAME} "
            f"under {install_prefix}/ and ln -sf into {PATH_BIN_DIR}/",
            flush=True,
        )
        return

    rc = install_files_via_ssh(
        ssh,
        install_prefix,
        [
            (S3_SCRIPT_NAME, s3_script, 0o755),
            (ADMIN_SCRIPT_NAME, admin_script, 0o755),
        ],
        dry_run=False,
    )
    if rc != 0:
        print(
            f"warning: failed to install garage-s3 scripts on {ssh} (exit {rc}).",
            file=sys.stderr,
        )
        return

    rc = install_files_via_ssh(
        ssh,
        config_dir,
        [(ENV_FILENAME, env_body, 0o600)],
        dry_run=False,
    )
    if rc != 0:
        print(
            f"warning: failed to install {ENV_FILENAME} on {ssh} (exit {rc}).",
            file=sys.stderr,
        )
        return

    link_cmd = (
        f"sudo mkdir -p {shlex.quote(PATH_BIN_DIR)} && "
        f"sudo ln -sfn {shlex.quote(s3_path)} {shlex.quote(f'{PATH_BIN_DIR}/{S3_SCRIPT_NAME}')} && "
        f"sudo ln -sfn {shlex.quote(admin_path)} {shlex.quote(f'{PATH_BIN_DIR}/{ADMIN_SCRIPT_NAME}')}"
    )
    link_rc = remote_run(ssh, link_cmd, dry_run=False)
    if link_rc != 0:
        print(
            f"warning: installed under {install_prefix}/ but could not symlink into "
            f"{PATH_BIN_DIR}/ (exit {link_rc}). Use {s3_path} directly.",
            file=sys.stderr,
        )
        print(
            f"On backup host: {s3_path} s3 ls s3://{bucket}/ --recursive",
            flush=True,
        )
        return

    print(
        f"On backup host: {S3_SCRIPT_NAME} s3 ls s3://{bucket}/ --recursive "
        f"(also {s3_path})",
        flush=True,
    )
