"""The cloud firewall `host create` puts on an environment's droplet."""

from catalpa_tooling import doctl_firewall as fw


def _ports(rules: str, protocol: str) -> set[str]:
    return {r.split(",")[1].removeprefix("ports:") for r in rules.split() if r.startswith(f"protocol:{protocol},")}


def test_default_lets_in_ssh_web_and_ping_and_out_only_deploy_traffic() -> None:
    """Docker-published ports got around ufw on the compromised staging server; this
    filters before the droplet, and nothing goes out on a port deploy hosts don't use."""
    inbound, outbound = fw.firewall_rules()
    assert _ports(inbound, "tcp") == {"22", "80", "443"}
    assert _ports(inbound, "udp") == {"68", "443"}
    assert "protocol:icmp," in inbound
    assert _ports(outbound, "tcp") == {"22", "53", "80", "443", "465", "587", "10051"}
    assert _ports(outbound, "udp") == {"53", "67", "123"}
    assert "all" not in _ports(outbound, "tcp") | _ports(outbound, "udp")


def test_an_env_can_add_outbound_ports_open_outbound_or_opt_out() -> None:
    _, more = fw.firewall_rules({"out_tcp": [5432]})
    assert "5432" in _ports(more, "tcp")
    _, opened = fw.firewall_rules({"out_open": True})
    assert _ports(opened, "tcp") == {"all"}
    assert fw.env_firewall_settings({"digitalocean": {"firewall": False}}) is None
    assert fw.env_firewall_settings({}) == {}


def test_makes_the_firewall_once_then_only_adds_the_droplet(monkeypatch) -> None:
    calls: list[list[str]] = []
    existing: list[dict] = []

    def fake(args, **_k):
        calls.append(list(args))
        return existing if args[:3] == ["compute", "firewall", "list"] else {}

    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl_json", fake)
    assert fw.ensure_droplet_firewall("site-prod", 7, {}, context="personal") == 0
    assert calls[-1][:5] == ["compute", "firewall", "create", "--name", "dk-site-prod"]
    existing.append({"id": "fw1", "name": "dk-site-prod", "droplet_ids": [3]})
    assert fw.ensure_droplet_firewall("site-prod", 7, {}, context="personal") == 0
    assert calls[-1] == ["compute", "firewall", "add-droplets", "fw1", "--droplet-ids", "7"]


def _fake_doctl(monkeypatch, existing, fail_on=None):
    from catalpa_tooling.doctl_binary import DoctlCommandError

    calls: list[list[str]] = []

    def fake(args, **_k):
        calls.append(list(args))
        if fail_on and args[:3] == ["compute", "firewall", fail_on]:
            raise DoctlCommandError("refused", returncode=5)
        return existing if args[:3] == ["compute", "firewall", "list"] else []

    monkeypatch.setattr("catalpa_tooling.doctl_binary.run_doctl_json", fake)
    return calls


def test_the_new_firewall_is_put_on_the_droplet(monkeypatch) -> None:
    calls = _fake_doctl(monkeypatch, [])
    assert fw.ensure_droplet_firewall("site", 7, {"out_udp": [41641]}, context=None) == 0
    create = calls[-1]
    assert create[create.index("--droplet-ids") + 1] == "7"
    assert "protocol:udp,ports:41641," in create[create.index("--outbound-rules") + 1]


def test_a_droplet_already_on_its_firewall_is_left_alone(monkeypatch) -> None:
    calls = _fake_doctl(monkeypatch, [{"id": "fw1", "name": "dk-site", "droplet_ids": [7]}])
    assert fw.ensure_droplet_firewall("site", 7, {}, context=None) == 0
    assert [c[:3] for c in calls] == [["compute", "firewall", "list"]]


def test_a_refused_firewall_is_reported_as_a_failure(monkeypatch) -> None:
    _fake_doctl(monkeypatch, [], fail_on="create")
    assert fw.ensure_droplet_firewall("site", 7, {}, context=None) == 5
