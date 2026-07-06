"""Export and display the local dev proxy CA for LAN device trust."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import TextIO

from catalpa_tooling.dev_lan_access import (
    LOCAL_PROXY_CA_HTTP_PATH,
    ca_download_url_for_ip,
    detect_dev_lan_ipv4,
)
from catalpa_tooling.local_proxy import local_proxy_data_dir, proxy_container_id
from catalpa_tooling.run_cmd import run as run_cmd
from catalpa_tooling.trust_caddy_cert import CADDY_LOCAL_CA_PATH


def export_proxy_ca_to_data_dir(*, dry_run: bool = False) -> Path | None:
    """Copy the running proxy's CA root to ``local_proxy_data_dir()/ca-root.crt``."""
    cid = proxy_container_id()
    if not cid:
        print(
            "Error: catalpa-local-proxy is not running. Start with `dk proxy up`.",
            file=sys.stderr,
        )
        return None
    dest = local_proxy_data_dir() / "ca-root.crt"
    if dry_run:
        print(f"dry-run: would copy CA from {cid} to {dest}", file=sys.stderr)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = run_cmd(
        ["docker", "cp", f"{cid}:{CADDY_LOCAL_CA_PATH}", str(dest)],
        check=False,
        print_cmd=False,
    )
    if result.returncode != 0:
        print("Failed to export local dev CA from the proxy container.", file=sys.stderr)
        return None
    return dest


def _print_qr(url: str, *, file: TextIO) -> None:
    try:
        import segno
    except ImportError:
        print("(Install segno for a terminal QR code: uv add segno)", file=file)
        return
    segno.make(url).terminal(out=file)


def print_proxy_ca_instructions(
    *,
    info: dict | None = None,
    dry_run: bool = False,
    file: TextIO | None = None,
) -> int:
    """Print CA download URL, QR code, and per-OS install steps for LAN devices."""
    out = sys.stderr if file is None else file
    ips = detect_dev_lan_ipv4()
    if not ips:
        print(
            "No LAN IPv4 address detected. Connect to Wi-Fi/Ethernet and retry.",
            file=out,
        )
        return 1

    if not dry_run:
        exported = export_proxy_ca_to_data_dir()
        if exported is None:
            return 1
        print(f"CA exported to {exported}", file=out)

    ca_url = ca_download_url_for_ip(ips[0], info)
    host_path = local_proxy_data_dir() / "ca-root.crt"

    print("", file=out)
    print("Install the Catalpa local dev CA on your phone/tablet (one time per device):", file=out)
    print("", file=out)
    print(f"  1. On the device (same Wi-Fi), open: {ca_url}", file=out)
    print(f"     Or copy from this machine: {host_path}", file=out)
    print("", file=out)
    print("  QR (CA download URL):", file=out)
    _print_qr(ca_url, file=out)
    print("", file=out)
    print("  2. iOS:", file=out)
    print("     - Open the URL, allow profile download, install in Settings > General > VPN & Device Management", file=out)
    print("     - Then Settings > General > About > Certificate Trust Settings > enable full trust", file=out)
    print("", file=out)
    print("  3. Android:", file=out)
    print("     - Open the URL, download the .crt, install via Settings > Security > Encryption & credentials", file=out)
    print("       > Install a certificate > CA certificate", file=out)
    print("", file=out)
    print(f"  4. Browse your LAN dev URLs (https://…). HTTP path {LOCAL_PROXY_CA_HTTP_PATH} only serves the CA.", file=out)
    if shutil.which("open"):
        print("", file=out)
        print(f"  On this Mac: open {ca_url}", file=out)
    return 0
