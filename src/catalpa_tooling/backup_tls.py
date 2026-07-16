"""Issue / install / status for private backup S3 TLS (CA + server cert).

Material lives in SOPS ``docker/envs/<env>/backup-tls.yaml``. Host install uses
``backup_docker_host`` (server + CA) and app ``docker_host`` (CA only).
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
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.sops_credentials import (
    SopsCommandError,
    SopsNotFoundError,
    decrypt_sops_yaml,
    ensure_sops_available,
    write_encrypted_yaml,
)
from catalpa_tooling.systemd_remote_install import parse_docker_host_to_ssh_target

BACKUP_TLS_FILENAME = "backup-tls.yaml"

KEY_CA_CRT = "backup_ca_crt"
KEY_CA_KEY = "backup_ca_key"
KEY_SERVER_CRT = "backup_server_crt"
KEY_SERVER_KEY = "backup_server_key"
KEY_SERVER_IPS = "backup_server_ips"
KEY_SERVER_DNS = "backup_server_dns"

HOST_CA_NAME = "backup-ca.crt"
HOST_SERVER_CRT_NAME = "backup-server.crt"
HOST_SERVER_KEY_NAME = "backup-server.key"

_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_DNS_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


def backup_tls_path(config: ProjectConfig, env_name: str) -> Path:
    return config.deploy_envs_dir / env_name / BACKUP_TLS_FILENAME


def backup_tls_host_dir(config: ProjectConfig) -> Path:
    """Absolute directory on deploy/backup hosts for installed PEMs."""
    return Path(config.ops.config_dir) / "tls"


def default_backup_ca_file(config: ProjectConfig) -> str:
    return str(backup_tls_host_dir(config) / HOST_CA_NAME)


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
        raise RuntimeError("openssl is required to issue backup TLS certificates")


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


def generate_backup_tls_material(
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
    cn = dns_list[0] if dns_list else "backup-tls"
    san_parts = [f"IP:{ip}" for ip in ip_list] + [f"DNS:{d}" for d in dns_list]
    san = ",".join(san_parts)

    with tempfile.TemporaryDirectory(prefix="catalpa-backup-tls-") as td:
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


def cmd_backup_tls_issue(
    config: ProjectConfig,
    env_name: str,
    *,
    ips: list[str],
    dns_names: list[str],
    days: int,
    force: bool,
    dry_run: bool,
) -> int:
    path = backup_tls_path(config, env_name)
    if path.is_file() and not force:
        print(
            f"{path} already exists; refuse to overwrite without --force.",
            file=sys.stderr,
        )
        return 1
    try:
        material = generate_backup_tls_material(ips=ips, dns_names=dns_names, days=days)
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
        "Next: point Caddy at the server cert on the backup host after "
        f"`dk {env_name} backup-tls install`.",
        flush=True,
    )
    return 0


def _pem_required(data: dict[str, Any], key: str) -> str:
    raw = data.get(key)
    if not isinstance(raw, str) or "BEGIN" not in raw:
        raise ValueError(f"Missing or invalid PEM for {key!r} in {BACKUP_TLS_FILENAME}")
    return raw


def _install_files_via_ssh(
    ssh_target: str,
    remote_dir: str,
    files: list[tuple[str, str, int]],
    *,
    dry_run: bool,
) -> int:
    """Install ``(filename, content, mode)`` under ``remote_dir`` on ``ssh_target``."""
    if dry_run:
        names = ", ".join(f"{n} mode={m:o}" for n, _, m in files)
        print(
            f"[dry-run] would install on {ssh_target}:{remote_dir}/ → {names}",
            flush=True,
        )
        return 0

    from catalpa_tooling.ssh_known_hosts import ensure_ssh_known_host_for_docker_host

    # Accept raw user@host or ssh:// —
    dh = ssh_target if "://" in ssh_target else f"ssh://{ssh_target}"
    kh = ensure_ssh_known_host_for_docker_host(dh)
    if kh != 0:
        print(
            f"Could not register SSH host key for {ssh_target!r}.",
            file=sys.stderr,
        )
        return 1

    mkdir = run_cmd(
        ["ssh", "-o", "BatchMode=yes", ssh_target, f"sudo mkdir -p {remote_dir}"],
        check=False,
        print_cmd=True,
    )
    if mkdir.returncode != 0:
        return int(mkdir.returncode or 1)

    with tempfile.TemporaryDirectory(prefix="catalpa-backup-tls-install-") as td:
        local_dir = Path(td)
        local_paths: list[Path] = []
        for name, content, _mode in files:
            p = local_dir / name
            p.write_text(content, encoding="utf-8")
            p.chmod(0o600)
            local_paths.append(p)
        # Stage under /tmp then sudo install with modes
        remote_stage = f"/tmp/catalpa-backup-tls-{Path(td).name}"
        stage_mkdir = run_cmd(
            ["ssh", "-o", "BatchMode=yes", ssh_target, f"mkdir -p {remote_stage}"],
            check=False,
            print_cmd=True,
        )
        if stage_mkdir.returncode != 0:
            return int(stage_mkdir.returncode or 1)
        scp = run_cmd(
            [
                "scp",
                "-q",
                "-o",
                "BatchMode=yes",
                *[str(p) for p in local_paths],
                f"{ssh_target}:{remote_stage}/",
            ],
            check=False,
            print_cmd=True,
        )
        if scp.returncode != 0:
            return int(scp.returncode or 1)

        install_parts = [f"sudo mkdir -p {remote_dir}"]
        for name, _content, mode in files:
            install_parts.append(
                f"sudo install -m {mode:o} {remote_stage}/{name} {remote_dir}/{name}"
            )
        install_parts.append(f"rm -rf {remote_stage}")
        remote_script = " && ".join(install_parts)
        fin = run_cmd(
            ["ssh", "-o", "BatchMode=yes", ssh_target, remote_script],
            check=False,
            print_cmd=True,
        )
        return int(fin.returncode or 0)


def cmd_backup_tls_install(
    config: ProjectConfig,
    env_name: str,
    *,
    dry_run: bool,
) -> int:
    info = _read_info(config, env_name)
    path = backup_tls_path(config, env_name)
    if not path.is_file():
        print(
            f"Missing {path}. Run `dk {env_name} backup-tls issue --ip …` first.",
            file=sys.stderr,
        )
        return 1

    docker_host = str(info.get("docker_host", "") or "").strip()
    backup_host = str(info.get("backup_docker_host", "") or "").strip()
    if not backup_host:
        print(
            f"backup_docker_host is unset in docker/envs/{env_name}/info.yaml; "
            "required to install server cert + CA on the backup host.",
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

    tls_dir = str(backup_tls_host_dir(config))
    ca_path = default_backup_ca_file(config)

    print(f"Installing CA + server cert on backup host {backup_ssh} → {tls_dir}/", flush=True)
    rc = _install_files_via_ssh(
        backup_ssh,
        tls_dir,
        [
            (HOST_CA_NAME, ca_crt, 0o644),
            (HOST_SERVER_CRT_NAME, srv_crt, 0o644),
            (HOST_SERVER_KEY_NAME, srv_key, 0o600),
        ],
        dry_run=dry_run,
    )
    if rc != 0:
        return rc

    print(f"Installing CA only on app host {app_ssh} → {tls_dir}/", flush=True)
    rc = _install_files_via_ssh(
        app_ssh,
        tls_dir,
        [(HOST_CA_NAME, ca_crt, 0o644)],
        dry_run=dry_run,
    )
    if rc != 0:
        return rc

    print(
        f"Installed under {tls_dir}/. "
        f"BACKUP_CA_FILE defaults to {ca_path} while {BACKUP_TLS_FILENAME} exists "
        f"(override in info.yaml env: if needed). Recreate the db service after install.",
        flush=True,
    )
    return 0


def _remote_path_exists(ssh_target: str, remote_path: str) -> bool | None:
    """True/False when SSH works; None on connection/command failure."""
    r = run_cmd(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            ssh_target,
            f"test -f {remote_path}",
        ],
        check=False,
        print_cmd=False,
        capture_output=True,
        text=True,
    )
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    return None


def cmd_backup_tls_status(
    config: ProjectConfig,
    env_name: str,
    *,
    check_remote: bool,
) -> int:
    path = backup_tls_path(config, env_name)
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
    backup_host = str(info.get("backup_docker_host", "") or "").strip()
    docker_host = str(info.get("docker_host", "") or "").strip()
    print(f"  backup_docker_host: {backup_host or '(unset)'}", flush=True)
    print(f"  docker_host: {docker_host or '(unset)'}", flush=True)
    tls_dir = backup_tls_host_dir(config)
    print(f"  install dir: {tls_dir}/", flush=True)
    print(
        f"  BACKUP_CA_FILE (inferred while {BACKUP_TLS_FILENAME} exists): "
        f"{default_backup_ca_file(config)}",
        flush=True,
    )

    if not check_remote:
        return 0

    for label, host, names in (
        ("backup", backup_host, (HOST_CA_NAME, HOST_SERVER_CRT_NAME, HOST_SERVER_KEY_NAME)),
        ("app", docker_host, (HOST_CA_NAME,)),
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
            remote = f"{tls_dir}/{name}"
            st = _remote_path_exists(ssh, remote)
            if st is True:
                print(f"  remote {label} {remote}: present", flush=True)
            elif st is False:
                print(f"  remote {label} {remote}: missing", flush=True)
            else:
                print(f"  remote {label} {remote}: unreachable", flush=True)
    return 0


def handle_backup_tls_command(
    ns: Any,
    config: ProjectConfig,
    env_name: str,
    *,
    dry_run: bool,
) -> int:
    """Dispatch ``dk <env> backup-tls …``."""
    sub = getattr(ns, "backup_tls_command", None)
    if sub == "issue":
        return cmd_backup_tls_issue(
            config,
            env_name,
            ips=list(getattr(ns, "ips", None) or []),
            dns_names=list(getattr(ns, "dns_names", None) or []),
            days=int(getattr(ns, "days", 825) or 825),
            force=bool(getattr(ns, "force", False)),
            dry_run=dry_run,
        )
    if sub == "install":
        return cmd_backup_tls_install(config, env_name, dry_run=dry_run)
    if sub == "status":
        return cmd_backup_tls_status(
            config,
            env_name,
            check_remote=bool(getattr(ns, "check_remote", False)),
        )
    print("usage: dk <env> backup-tls {issue,install,status}", file=sys.stderr)
    return 2
