"""Issue / install / status for closed-DC backup TLS (CA + server cert).

Material lives in SOPS ``docker/envs/<env>/dc-backup-tls.yaml``.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from catalpa_tooling.config import ProjectConfig
from catalpa_tooling.dc_backup.paths import (
    APP_CA_FILENAME,
    DC_BACKUP_TLS_FILENAME,
    GARAGE_TLS_CA_NAME,
    GARAGE_TLS_DIR,
    GARAGE_TLS_SERVER_CRT_NAME,
    GARAGE_TLS_SERVER_KEY_NAME,
    INFO_DC_BACKUP_DOCKER_HOST,
)
from catalpa_tooling.dc_backup.ssh_install import install_files_via_ssh, remote_path_exists
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.sops_credentials import (
    SopsCommandError,
    SopsNotFoundError,
    decrypt_sops_yaml,
    ensure_sops_available,
    write_encrypted_yaml,
)
from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

KEY_CA_CRT = "backup_ca_crt"
KEY_CA_KEY = "backup_ca_key"
KEY_SERVER_CRT = "backup_server_crt"
KEY_SERVER_KEY = "backup_server_key"
KEY_SERVER_IPS = "backup_server_ips"
KEY_SERVER_DNS = "backup_server_dns"

_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_DNS_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


def dc_backup_tls_path(config: ProjectConfig, env_name: str) -> Path:
    return config.deploy_envs_dir / env_name / DC_BACKUP_TLS_FILENAME


def app_tls_host_dir(config: ProjectConfig) -> Path:
    """App deploy host directory for the CA PEM."""
    return Path(config.ops.config_dir) / "tls"


def default_dc_backup_ca_file(config: ProjectConfig) -> str:
    return str(app_tls_host_dir(config) / APP_CA_FILENAME)


def _read_info(config: ProjectConfig, env_name: str) -> dict[str, Any]:
    info_path = config.deploy_envs_dir / env_name / "info.yaml"
    if not info_path.is_file():
        raise FileNotFoundError(f"Missing {info_path}")
    with open(info_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _normalize_ip_list(ips: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in ips:
        ip = (raw or "").strip()
        if not ip:
            continue
        if not _IPV4_RE.match(ip):
            raise ValueError(f"Invalid IPv4 address: {ip!r}")
        parts = ip.split(".")
        if any(int(p) > 255 for p in parts):
            raise ValueError(f"Invalid IPv4 address: {ip!r}")
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    if not out:
        raise ValueError("At least one --ip is required")
    return out


def _normalize_dns_list(names: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = (raw or "").strip().lower()
        if not name:
            continue
        if not _DNS_RE.match(name):
            raise ValueError(f"Invalid DNS name: {name!r}")
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _ensure_openssl() -> None:
    if not shutil.which("openssl"):
        raise RuntimeError("openssl is required to issue dc-backup TLS certificates")


def _run_openssl(argv: list[str], *, cwd: Path | None = None) -> None:
    r = run_cmd(
        ["openssl", *argv],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        print_cmd=False,
    )
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip() or "openssl failed"
        raise RuntimeError(err)


def generate_dc_backup_tls_material(
    *,
    ips: list[str],
    dns_names: list[str] | None = None,
    days: int = 825,
) -> dict[str, Any]:
    """Generate CA + server PEMs with openssl. Does not print private keys."""
    _ensure_openssl()
    if days < 1:
        raise ValueError("--days must be >= 1")
    ip_list = _normalize_ip_list(ips)
    dns_list = _normalize_dns_list(list(dns_names or []))
    cn = dns_list[0] if dns_list else "dc-backup"
    san_parts = [f"IP:{ip}" for ip in ip_list] + [f"DNS:{d}" for d in dns_list]
    san = ",".join(san_parts)

    with tempfile.TemporaryDirectory(prefix="catalpa-dc-backup-tls-") as td:
        root = Path(td)
        ca_key = root / "ca.key"
        ca_crt = root / "ca.crt"
        srv_key = root / "server.key"
        srv_csr = root / "server.csr"
        srv_crt = root / "server.crt"
        ext = root / "san.ext"
        ext.write_text(
            "basicConstraints=CA:FALSE\n"
            "keyUsage=digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            f"subjectAltName={san}\n",
            encoding="utf-8",
        )

        _run_openssl(
            [
                "req",
                "-x509",
                "-newkey",
                "rsa:4096",
                "-sha256",
                "-days",
                str(days),
                "-nodes",
                "-keyout",
                str(ca_key),
                "-out",
                str(ca_crt),
                "-subj",
                f"/CN={cn}-ca",
            ]
        )
        _run_openssl(
            [
                "req",
                "-newkey",
                "rsa:2048",
                "-sha256",
                "-nodes",
                "-keyout",
                str(srv_key),
                "-out",
                str(srv_csr),
                "-subj",
                f"/CN={cn}",
            ]
        )
        _run_openssl(
            [
                "x509",
                "-req",
                "-in",
                str(srv_csr),
                "-CA",
                str(ca_crt),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-out",
                str(srv_crt),
                "-days",
                str(days),
                "-sha256",
                "-extfile",
                str(ext),
            ]
        )

        return {
            KEY_CA_CRT: ca_crt.read_text(encoding="utf-8"),
            KEY_CA_KEY: ca_key.read_text(encoding="utf-8"),
            KEY_SERVER_CRT: srv_crt.read_text(encoding="utf-8"),
            KEY_SERVER_KEY: srv_key.read_text(encoding="utf-8"),
            KEY_SERVER_IPS: ip_list,
            KEY_SERVER_DNS: dns_list,
        }


def cmd_dc_backup_tls_issue(
    config: ProjectConfig,
    env_name: str,
    *,
    ips: list[str],
    dns_names: list[str],
    days: int,
    force: bool,
    dry_run: bool,
) -> int:
    path = dc_backup_tls_path(config, env_name)
    if path.is_file() and not force:
        print(
            f"{path} already exists; refuse to overwrite without --force.",
            file=sys.stderr,
        )
        return 1
    try:
        material = generate_dc_backup_tls_material(ips=ips, dns_names=dns_names, days=days)
    except (ValueError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 1

    if dry_run:
        print(
            f"dry-run: would write SOPS {path} "
            f"(ips={material[KEY_SERVER_IPS]!r}, dns={material[KEY_SERVER_DNS]!r}, days={days})",
            flush=True,
        )
        return 0

    try:
        ensure_sops_available()
        write_encrypted_yaml(path, material)
    except (SopsNotFoundError, SopsCommandError) as e:
        print(str(e), file=sys.stderr)
        return 1

    print(
        f"Wrote SOPS {path} "
        f"(ips={material[KEY_SERVER_IPS]!r}, dns={material[KEY_SERVER_DNS]!r}). "
        "Private keys were not printed.",
        flush=True,
    )
    print(
        f"Next: `dk {env_name} dc-backup tls install`, then "
        f"`dk {env_name} dc-backup bootstrap`, `install --up`, and `provision`.",
        flush=True,
    )
    return 0


def _pem_required(data: dict[str, Any], key: str) -> str:
    raw = data.get(key)
    if not isinstance(raw, str) or "BEGIN" not in raw:
        raise ValueError(f"Missing or invalid PEM for {key!r} in {DC_BACKUP_TLS_FILENAME}")
    return raw


def cmd_dc_backup_tls_install(
    config: ProjectConfig,
    env_name: str,
    *,
    dry_run: bool,
) -> int:
    info = _read_info(config, env_name)
    path = dc_backup_tls_path(config, env_name)
    if not path.is_file():
        print(
            f"Missing {path}. Run `dk {env_name} dc-backup tls issue --ip …` first.",
            file=sys.stderr,
        )
        return 1

    docker_host = str(info.get("docker_host", "") or "").strip()
    backup_host = str(info.get(INFO_DC_BACKUP_DOCKER_HOST, "") or "").strip()
    if not backup_host:
        print(
            f"{INFO_DC_BACKUP_DOCKER_HOST} is unset in docker/envs/{env_name}/info.yaml; "
            "required to install server cert + CA on the DC backup host.",
            file=sys.stderr,
        )
        return 1
    if not docker_host:
        print(
            f"docker_host is unset in docker/envs/{env_name}/info.yaml; "
            "required to install the CA on the app deploy host.",
            file=sys.stderr,
        )
        return 1

    try:
        backup_ssh = parse_docker_host_to_ssh_target(backup_host)
        app_ssh = parse_docker_host_to_ssh_target(docker_host)
        data = decrypt_sops_yaml(path)
        ca_crt = _pem_required(data, KEY_CA_CRT)
        srv_crt = _pem_required(data, KEY_SERVER_CRT)
        srv_key = _pem_required(data, KEY_SERVER_KEY)
    except (ValueError, SopsNotFoundError, SopsCommandError) as e:
        print(str(e), file=sys.stderr)
        return 1

    app_dir = str(app_tls_host_dir(config))
    ca_path = default_dc_backup_ca_file(config)

    print(
        f"Installing CA + server cert on DC backup host {backup_ssh} → {GARAGE_TLS_DIR}/",
        flush=True,
    )
    rc = install_files_via_ssh(
        backup_ssh,
        GARAGE_TLS_DIR,
        [
            (GARAGE_TLS_CA_NAME, ca_crt, 0o644),
            (GARAGE_TLS_SERVER_CRT_NAME, srv_crt, 0o644),
            (GARAGE_TLS_SERVER_KEY_NAME, srv_key, 0o600),
        ],
        dry_run=dry_run,
    )
    if rc != 0:
        return rc

    print(f"Installing CA only on app host {app_ssh} → {app_dir}/", flush=True)
    rc = install_files_via_ssh(
        app_ssh,
        app_dir,
        [(APP_CA_FILENAME, ca_crt, 0o644)],
        dry_run=dry_run,
    )
    if rc != 0:
        return rc

    from catalpa_tooling.dc_backup.hosts import DC_BACKUP_CA_FILE_ENV

    print(
        f"Installed. {DC_BACKUP_CA_FILE_ENV} defaults to {ca_path} while "
        f"{DC_BACKUP_TLS_FILENAME} exists (override in info.yaml env: if needed). "
        f"Next: `dk {env_name} dc-backup bootstrap` (if needed), "
        f"`install --up`, then `provision`. Recreate the db service after CA install.",
        flush=True,
    )
    return 0


def cmd_dc_backup_tls_status(
    config: ProjectConfig,
    env_name: str,
    *,
    check_remote: bool,
) -> int:
    path = dc_backup_tls_path(config, env_name)
    print(f"SOPS file: {path}", flush=True)
    if not path.is_file():
        print("  exists: no", flush=True)
        return 1
    print("  exists: yes", flush=True)

    try:
        data = decrypt_sops_yaml(path)
    except (SopsNotFoundError, SopsCommandError) as e:
        print(f"  decrypt: failed ({e})", file=sys.stderr)
        return 1

    ips = data.get(KEY_SERVER_IPS) or []
    dns = data.get(KEY_SERVER_DNS) or []
    print(f"  server SANs: ips={ips!r} dns={dns!r}", flush=True)
    for key in (KEY_CA_CRT, KEY_CA_KEY, KEY_SERVER_CRT, KEY_SERVER_KEY):
        present = isinstance(data.get(key), str) and "BEGIN" in str(data.get(key))
        print(f"  {key}: {'present' if present else 'missing'}", flush=True)

    info = _read_info(config, env_name)
    backup_host = str(info.get(INFO_DC_BACKUP_DOCKER_HOST, "") or "").strip()
    docker_host = str(info.get("docker_host", "") or "").strip()
    print(f"  {INFO_DC_BACKUP_DOCKER_HOST}: {backup_host or '(unset)'}", flush=True)
    print(f"  docker_host: {docker_host or '(unset)'}", flush=True)
    print(f"  garage TLS dir: {GARAGE_TLS_DIR}/", flush=True)
    print(f"  app TLS dir: {app_tls_host_dir(config)}/", flush=True)
    print(
        f"  DC_BACKUP_CA_FILE (inferred while {DC_BACKUP_TLS_FILENAME} exists): "
        f"{default_dc_backup_ca_file(config)}",
        flush=True,
    )

    if not check_remote:
        return 0

    app_dir = str(app_tls_host_dir(config))
    for label, host, base, names in (
        (
            "dc-backup",
            backup_host,
            GARAGE_TLS_DIR,
            (GARAGE_TLS_CA_NAME, GARAGE_TLS_SERVER_CRT_NAME, GARAGE_TLS_SERVER_KEY_NAME),
        ),
        ("app", docker_host, app_dir, (APP_CA_FILENAME,)),
    ):
        if not host:
            print(f"  remote {label}: skipped (host unset)", flush=True)
            continue
        try:
            ssh = parse_docker_host_to_ssh_target(host)
        except ValueError as e:
            print(f"  remote {label}: invalid host ({e})", flush=True)
            continue
        for name in names:
            remote = f"{base}/{name}"
            st = remote_path_exists(ssh, remote)
            if st is True:
                print(f"  remote {label} {remote}: present", flush=True)
            elif st is False:
                print(f"  remote {label} {remote}: missing", flush=True)
            else:
                print(f"  remote {label} {remote}: unreachable", flush=True)
    return 0
