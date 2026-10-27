"""The DigitalOcean cloud firewall `host create` puts on an environment's droplet.

One firewall per droplet, named ``dk-<droplet>``, made once and the droplet added to it
by ID. The same defaults as containerwharf's hosts (decided 2026-10-27, after a staging
server that Docker-published ports had left open was used for a miner and a
bandwidth-resale tunnel):

- in: SSH, HTTP and HTTPS (and HTTP/3) from anywhere, ping, and DHCP replies;
- out: only what deploy hosts use (``OUT_TCP``, ``OUT_UDP``), plus ping.

DigitalOcean filters before the droplet, so this holds even where Docker's own rules
would get around ufw. ``info.yaml`` ``digitalocean.firewall: false`` makes none; a
mapping can add outbound ports (``out_tcp``, ``out_udp``) or open outbound
(``out_open: true``).
"""

from __future__ import annotations

import sys
from typing import Any

ANYWHERE = "address:0.0.0.0/0,address:::/0"
IN_TCP = (22, 80, 443)
#: HTTP/3, and DHCP replies: a DO firewall drops those otherwise, and the droplet
#: comes back from its next reboot with no address.
IN_UDP = (68, 443)
#: git over SSH, DNS, HTTP(S) (images, updates, ACME, backups), mail, Zabbix.
OUT_TCP = (22, 53, 80, 443, 465, 587, 10051)
#: DNS, DHCP requests, NTP.
OUT_UDP = (53, 67, 123)


def firewall_name(droplet_name: str) -> str:
    return f"dk-{droplet_name}"


def firewall_rules(settings: dict[str, Any] | None = None) -> tuple[str, str]:
    """``(inbound, outbound)`` in ``doctl compute firewall create`` rule syntax."""
    settings = settings or {}
    inbound = [f"protocol:tcp,ports:{p},{ANYWHERE}" for p in IN_TCP]
    inbound += [f"protocol:udp,ports:{p},{ANYWHERE}" for p in IN_UDP]
    inbound.append(f"protocol:icmp,{ANYWHERE}")
    if settings.get("out_open"):
        outbound = [f"protocol:tcp,ports:all,{ANYWHERE}", f"protocol:udp,ports:all,{ANYWHERE}"]
    else:
        tcp = sorted({*OUT_TCP, *(int(p) for p in settings.get("out_tcp") or [])})
        udp = sorted({*OUT_UDP, *(int(p) for p in settings.get("out_udp") or [])})
        outbound = [f"protocol:tcp,ports:{p},{ANYWHERE}" for p in tcp]
        outbound += [f"protocol:udp,ports:{p},{ANYWHERE}" for p in udp]
    outbound.append(f"protocol:icmp,{ANYWHERE}")
    return " ".join(inbound), " ".join(outbound)


def env_firewall_settings(info: dict[str, Any]) -> dict[str, Any] | None:
    """``info.yaml`` ``digitalocean.firewall``: None for ``false``, else its settings."""
    raw = info.get("digitalocean")
    value = raw.get("firewall", {}) if isinstance(raw, dict) else {}
    if value is False:
        return None
    return value if isinstance(value, dict) else {}


def ensure_droplet_firewall(
    droplet_name: str,
    droplet_id: int | str,
    settings: dict[str, Any],
    *,
    context: str | None,
    dry_run: bool = False,
) -> int:
    """Make ``dk-<droplet>`` with the default rules, or add the droplet to it."""
    from catalpa_tooling.doctl_binary import DoctlCommandError, run_doctl_json

    name = firewall_name(droplet_name)
    inbound, outbound = firewall_rules(settings)
    if dry_run:
        print(f"dry-run: firewall {name}: in {inbound} | out {outbound}", file=sys.stderr)
        return 0
    try:
        existing = run_doctl_json(["compute", "firewall", "list"], context=context)
        found = [f for f in existing if isinstance(f, dict) and f.get("name") == name] if isinstance(existing, list) else []
        if found:
            if int(droplet_id) in [int(i) for i in found[0].get("droplet_ids") or []]:
                print(f"Firewall {name} already covers the droplet.", file=sys.stderr)
                return 0
            run_doctl_json(
                ["compute", "firewall", "add-droplets", str(found[0]["id"]), "--droplet-ids", str(droplet_id)],
                context=context,
            )
            print(f"Added the droplet to firewall {name}.", file=sys.stderr)
            return 0
        run_doctl_json(
            [
                "compute", "firewall", "create", "--name", name,
                "--inbound-rules", inbound, "--outbound-rules", outbound,
                "--droplet-ids", str(droplet_id),
            ],
            context=context,
        )  # fmt: skip
    except DoctlCommandError as e:
        print(f"Firewall {name}: {e}", file=sys.stderr)
        return e.returncode or 1
    print(f"Made firewall {name}: SSH, web and ping in; deploy traffic out.", file=sys.stderr)
    return 0
