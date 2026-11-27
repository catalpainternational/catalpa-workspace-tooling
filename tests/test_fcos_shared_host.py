"""A server shared by several envs: ``docker/hosts/<host>/host.yaml`` and ``dk host <dir> render``.

Every account comes from the server's first boot; each has a slot that fixes its uid,
subordinate IDs and loopback ports (docs/MULTI_ENV_HOST.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from catalpa_tooling.fcos.settings import SUBUID_COUNT, SUBUID_START
from catalpa_tooling.fcos.shared_host import (
    EDGE_SLOT,
    HostConfigError,
    load_host,
    render_host,
)

ADMIN = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAdminAdminAdminAdminAdminAdminAdminAdmin admin"
DK = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDkDkDkDkDkDkDkDkDkDkDkDkDkDkDkDkDkDkDkDk dk"


def _host(tmp_path: Path, **overrides) -> Path:
    doc = {
        "hostname": "shared-1",
        "timezone": "Asia/Dili",
        "admin_keys": [ADMIN],
        "ssh_authorized_keys": [DK],
        "accounts": [
            {"name": "deploy", "slot": 1, "project": "nextgen", "env": "staging",
             "names": ["nextgen.example.test"]},
            {"name": "site", "slot": 2, "project": "partisipa", "env": "staging", "role": "site",
             "names": ["partisipa.example.test", "metabase.partisipa.example.test"]},
            {"name": "blue", "slot": 3, "project": "partisipa", "env": "staging", "role": "blue"},
            {"name": "green", "slot": 4, "project": "partisipa", "env": "staging", "role": "green"},
        ],
    }  # fmt: skip
    doc.update(overrides)
    d = tmp_path / "shared-1"
    d.mkdir(exist_ok=True)
    (d / "host.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return d


def test_slot_fixes_uid_subuid_and_ports(tmp_path):
    host = load_host(_host(tmp_path))
    blue = host.account("blue")
    assert blue.uid == 1003
    assert blue.subuid_start == SUBUID_START + 2 * SUBUID_COUNT
    assert blue.port() == 20030 and blue.port(4) == 20034
    assert host.edge.uid == 1000 + EDGE_SLOT


def test_accounts_of_an_env(tmp_path):
    host = load_host(_host(tmp_path))
    roles = host.accounts_of("partisipa", "staging")
    assert sorted(roles) == ["blue", "green", "site"]
    assert host.accounts_of("nextgen", "staging")["env"].name == "deploy"
    assert host.accounts_of("nextgen", "production") == {}


@pytest.mark.parametrize(
    ("accounts", "message"),
    [
        ([{"name": "a", "slot": 1, "project": "p", "env": "e"},
          {"name": "b", "slot": 1, "project": "p", "env": "f"}], "slot 1"),
        ([{"name": "a", "slot": 1, "project": "p", "env": "e"},
          {"name": "a", "slot": 2, "project": "p", "env": "f"}], "twice"),
        ([{"name": "core", "slot": 1, "project": "p", "env": "e"}], "core"),
        ([{"name": "edge", "slot": 1, "project": "p", "env": "e"}], "edge"),
        ([{"name": "a", "slot": EDGE_SLOT, "project": "p", "env": "e"}], "slot"),
        ([{"name": "a", "slot": 1, "project": "p", "env": "e", "role": "site"}], "blue"),
        ([{"name": "a", "slot": 1, "project": "p", "env": "e"},
          {"name": "b", "slot": 2, "project": "p", "env": "e"}], "one account"),
        ([{"name": "a", "slot": 5, "project": "p", "env": "e"}], "retired"),
        ([{"name": "a", "slot": 1, "project": "p", "env": "e", "names": ["x.test"]},
          {"name": "b", "slot": 2, "project": "p", "env": "f", "names": ["x.test"]}], "x.test"),
    ],
)  # fmt: skip
def test_invalid_hosts_are_refused(tmp_path, accounts, message):
    d = _host(tmp_path, accounts=accounts, retired_slots=[5])
    with pytest.raises(HostConfigError, match=message):
        load_host(d)


def test_colour_accounts_take_no_names(tmp_path):
    doc = yaml.safe_load((_host(tmp_path) / "host.yaml").read_text())
    doc["accounts"][2]["names"] = ["blue.example.test"]
    (tmp_path / "shared-1" / "host.yaml").write_text(yaml.safe_dump(doc))
    with pytest.raises(HostConfigError, match="site account"):
        load_host(tmp_path / "shared-1")


def test_render_makes_every_account_at_first_boot(tmp_path):
    files = render_host(load_host(_host(tmp_path)))
    bu = yaml.safe_load(files["host.bu"])
    users = {u["name"]: u.get("uid") for u in bu["passwd"]["users"]}
    assert users == {"core": None, "deploy": 1001, "site": 1002, "blue": 1003, "green": 1004,
                     "edge": 1000 + EDGE_SLOT}  # fmt: skip
    by_path = {f["path"]: f for f in bu["storage"]["files"]}
    subuid = by_path["/etc/subuid"]["contents"]["inline"]
    assert f"blue:{SUBUID_START + 2 * SUBUID_COUNT}:{SUBUID_COUNT}" in subuid
    assert "AllowUsers core deploy site blue green\n" in by_path["/etc/ssh/sshd_config.d/10-hardening.conf"]["contents"]["inline"]
    for name in ("deploy", "site", "blue", "green", "edge"):
        assert f"/var/lib/systemd/linger/{name}" in by_path
    assert DK in by_path["/var/home/blue/.ssh/authorized_keys.d/dk"]["contents"]["inline"]
    assert ADMIN in by_path["/var/home/core/.ssh/authorized_keys.d/dk"]["contents"]["inline"]
    assert (
        by_path["/var/home/green/.config/environment.d/10-registry-auth.conf"]["contents"]["inline"]
        == "REGISTRY_AUTH_FILE=/var/home/green/.config/containers/auth.json\n"
    )


def test_quadlet_folders_are_readable_only_by_their_account(tmp_path):
    bu = yaml.safe_load(render_host(load_host(_host(tmp_path)))["host.bu"])
    dirs = {d["path"]: d for d in bu["storage"]["directories"]}
    d = dirs["/etc/containers/systemd/users/1003"]
    assert d["mode"] == 0o750 and d["group"] == {"name": "blue"} and "user" not in d


def test_edge_routes_each_name_to_its_account(tmp_path):
    files = render_host(load_host(_host(tmp_path)))
    caddy = json.loads(files["edge/caddy.json"])
    routes = {
        tuple(r["match"][0]["host"]): r["handle"][0]["upstreams"][0]["dial"]
        for srv in caddy["apps"]["http"]["servers"].values()
        for r in srv.get("routes", [])
        if r.get("match")
    }
    assert routes == {
        ("nextgen.example.test",): "127.0.0.1:20010",
        ("partisipa.example.test", "metabase.partisipa.example.test"): "127.0.0.1:20020",
    }


def test_firewall_keeps_other_envs_off_an_accounts_ports(tmp_path):
    nft = render_host(load_host(_host(tmp_path)))["firewall/loopback.nft"]
    edge = 1000 + EDGE_SLOT
    assert f"tcp dport 20010-20019 meta skuid != {{ 1001, {edge} }}" in nft
    assert f"tcp dport 20020-20029 meta skuid != {{ 1002, 1003, 1004, {edge} }}" in nft
    assert f"tcp dport 20030-20039 meta skuid != {{ 1002, 1003, 1004, {edge} }}" in nft


def test_render_is_stable_and_names_its_source(tmp_path):
    host = load_host(_host(tmp_path))
    assert render_host(host) == render_host(host)
    manifest = json.loads(render_host(host)["manifest.json"])
    assert manifest["accounts"]["green"] == {
        "slot": 4, "uid": 1004, "subuid_start": SUBUID_START + 3 * SUBUID_COUNT,
        "ports": [20040, 20049], "project": "partisipa", "env": "staging", "role": "green",
    }  # fmt: skip


def test_insecure_registries_only_when_listed(tmp_path):
    bu = render_host(load_host(_host(tmp_path)))["host.bu"]
    assert "registries.conf.d" not in bu
    bu = yaml.safe_load(render_host(load_host(_host(tmp_path, insecure_registries=["10.0.2.2:15000"])))["host.bu"])
    by_path = {f["path"]: f for f in bu["storage"]["files"]}
    conf = by_path["/etc/containers/registries.conf.d/50-dk-insecure.conf"]["contents"]["inline"]
    assert 'location = "10.0.2.2:15000"' in conf and "insecure = true" in conf


# --- an env on a shared server ----------------------------------------------------------------

import shutil

from catalpa_tooling.config import load_project_config
from catalpa_tooling.fcos.render import RenderError, render_env
from tests.test_fcos_render import FIXTURES, _fake_adapter, needs_compose

HOST_OWNED = ("hostname", "admin_keys", "ssh_authorized_keys")


def _site_on_shared_host(tmp_path: Path, *, keep: tuple[str, ...] = (), accounts=None) -> Path:
    """The ``site`` fixture's staging env, moved onto a shared server as account ``web``."""
    root = tmp_path / "site"
    shutil.copytree(FIXTURES / "site", root, ignore=shutil.ignore_patterns("expected"))
    accounts = accounts or [
        {"name": "other", "slot": 1, "project": "nextgen", "env": "staging"},
        {"name": "web", "slot": 6, "project": "site", "env": "staging", "names": ["staging.site.example"]},
    ]
    _host(tmp_path / "hosts", accounts=accounts) if (tmp_path / "hosts").mkdir() is None else None
    info_path = root / "docker" / "envs" / "staging" / "info.yaml"
    info = yaml.safe_load(info_path.read_text())
    for key in HOST_OWNED:
        if key not in keep:
            info["fcos"].pop(key, None)
    info["host"] = "../hosts/shared-1"
    info["docker_host"] = "ssh://web@shared-1.example/run/user/1006/podman/podman.sock"
    info_path.write_text(yaml.safe_dump(info))
    return root


@needs_compose
def test_env_on_a_shared_host_takes_its_account_from_the_slot(tmp_path):
    render = render_env(load_project_config(_site_on_shared_host(tmp_path)), "staging", adapter=_fake_adapter())
    manifest = json.loads(render.files["manifest.json"])
    assert (manifest["user"], manifest["uid"], manifest["host"]) == ("web", 1006, "shared-1")
    assert manifest["subuid"]["start"] == SUBUID_START + 5 * SUBUID_COUNT
    assert manifest["config_dir"].endswith("/staging")
    for spec in manifest["bind_volumes"].values():
        assert spec["path"].startswith("/var/mnt/data/web/")
        if "owner_uid" in spec:
            assert spec["owner_uid"] >= SUBUID_START + 5 * SUBUID_COUNT
    # The server owns host.bu, core's keys and the account's keys.
    assert "host.bu" not in render.files
    dests = {e["dest"] for e in manifest["install"]}
    assert not any("authorized_keys" in d for d in dests)
    # Behind the shared edge: plain HTTP on the account's port, the edge's address trusted.
    socket = next(t for p, t in render.files.items() if p.endswith(".socket"))
    assert "ListenStream=127.0.0.1:20060" in socket and ":443" not in socket
    caddy = json.loads(render.files["caddy/caddy.json"])
    (srv,) = caddy["apps"]["http"]["servers"].values()
    assert srv["listen"] == ["fd/3"] and srv["trusted_proxies"]["ranges"] == ["127.0.0.1/32", "::1/128"]
    assert "tls" not in caddy["apps"]
    assert manifest["links"] == {
        f"/var/home/web/.config/systemd/user/sockets.target.wants/{manifest['proxy']}.socket": f"../{manifest['proxy']}.socket"
    }


@needs_compose
@pytest.mark.parametrize("key", HOST_OWNED)
def test_env_on_a_shared_host_refuses_what_the_host_owns(tmp_path, key):
    root = _site_on_shared_host(tmp_path, keep=(key,))
    with pytest.raises(RenderError, match=f"fcos.{key}"):
        render_env(load_project_config(root), "staging", adapter=_fake_adapter())


@needs_compose
def test_env_without_an_account_on_the_host_is_refused(tmp_path):
    root = _site_on_shared_host(tmp_path, accounts=[{"name": "other", "slot": 1, "project": "nextgen", "env": "staging"}])
    with pytest.raises(RenderError, match="no account for site staging"):
        render_env(load_project_config(root), "staging", adapter=_fake_adapter())


def test_a_certificate_authority_line_is_kept_through_a_rebuild(tmp_path):
    ca = 'cert-authority,principals="ops" ' + DK.replace(" dk", " ops-ca")
    bu = yaml.safe_load(render_host(load_host(_host(tmp_path, admin_keys=[ADMIN, ca])))["host.bu"])
    by_path = {f["path"]: f for f in bu["storage"]["files"]}
    assert ca in by_path["/var/home/core/.ssh/authorized_keys.d/dk"]["contents"]["inline"]
    with pytest.raises(HostConfigError, match="public keys"):
        load_host(_host(tmp_path, admin_keys=["command=\"x\" " + DK]))


def test_the_edge_has_no_admin_api_on_the_hosts_loopback(tmp_path):
    caddy = json.loads(render_host(load_host(_host(tmp_path)))["edge/caddy.json"])
    assert caddy["admin"] == {"disabled": True}


class _Host:
    """``Host.ssh`` that answers ``getent passwd`` as told."""

    address = "shared-1.example"

    def __init__(self, exists: bool, uid: int = 1005):
        self.exists, self.uid, self.calls = exists, uid, []

    def ssh(self, login, command, *, input=None, check=True):  # noqa: ARG002
        self.calls.append((login, command))
        import subprocess

        if not self.exists:
            return subprocess.CompletedProcess([], 1, b"", b"id: 'training': no such user")
        return subprocess.CompletedProcess([], 0, f"{self.uid}\n".encode(), b"")


def test_dk_refuses_an_env_whose_account_the_server_lacks():
    from catalpa_tooling.fcos.host import Committed, HostError, check_account

    shared = Committed(manifest={"user": "training", "uid": 1005, "host": "shared-1"}, files={})
    with pytest.raises(HostError, match="first boot"):
        check_account(_Host(exists=False), shared)
    check_account(_Host(exists=True), shared)
    with pytest.raises(HostError, match="is docker_host the right server"):
        check_account(_Host(exists=True, uid=1007), shared)
    own = _Host(exists=False)
    check_account(own, Committed(manifest={"user": "deploy", "uid": 1001}, files={}))
    assert own.calls == []  # a server of its own: nothing to check


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"timezone": "Asia/Dili\n    - path: /etc/x"}, "timezone"),
        ({"edge": {"image": "caddy:2\nExec=sh"}}, "edge.image"),
        ({"insecure_registries": ['x"\ninsecure = true']}, "insecure_registries"),
        ({"edge": {"email": "ops@x\n    - path: /etc/y"}}, "edge.email"),
    ],
)
def test_host_yaml_values_written_into_butane_are_one_line(tmp_path, override, message):
    with pytest.raises(HostConfigError, match=message):
        load_host(_host(tmp_path, **override))


@needs_compose
def test_monitoring_is_refused_on_a_shared_host_until_it_is_built(tmp_path):
    root = _site_on_shared_host(tmp_path)
    info_path = root / "docker" / "envs" / "staging" / "info.yaml"
    info = yaml.safe_load(info_path.read_text())
    info["monitoring"] = "zabbix"
    info_path.write_text(yaml.safe_dump(info))
    with pytest.raises(RenderError, match="isn't built for a shared server"):
        render_env(load_project_config(root), "staging", adapter=_fake_adapter())


def test_image_references_may_have_uppercase_tags(tmp_path):
    assert load_host(_host(tmp_path, edge={"image": "docker.io/library/caddy:2.8-Alpine"})).edge_image.endswith("Alpine")
