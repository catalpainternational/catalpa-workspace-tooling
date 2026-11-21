"""``dk <env> host destroy`` (#69, tooling 2.0 M2b), with doctl and SSH faked."""

from __future__ import annotations

from pathlib import Path

import pytest

from catalpa_tooling import host_destroy as hd
from catalpa_tooling.config import load_project_config
from catalpa_tooling.doctl_domains import HostDnsTarget
from tests.helpers import write_minimal_tooling_tree

IP = "203.0.113.7"
DROPLET = {"id": 42, "name": "x-staging", "region": {"slug": "sgp1"}, "networks": {"v4": [{"type": "public", "ip_address": IP}]}}
VOLUME = {"id": "vol-1", "name": "fcos-data", "size_gigabytes": 10, "droplet_ids": [42]}

QUADLET_INFO = """\
name: staging
# a comment that must survive
site_origin:
  - staging.example.org
docker_host: ssh://deploy@{host}/run/user/1001/podman/podman.sock
runtime: quadlet
fcos:
  hostname: fcos
digitalocean:
  droplet_name: x-staging
{extra}"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    write_minimal_tooling_tree(tmp_path)
    d = tmp_path / "docker" / "envs" / "staging"
    d.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    def make(host: str = IP, extra: str = "  allow_destroy: true\n"):
        (d / "info.yaml").write_text(QUADLET_INFO.format(host=host, extra=extra), encoding="utf-8")
        return load_project_config(tmp_path), d / "info.yaml"

    return make


@pytest.fixture
def doctl(monkeypatch: pytest.MonkeyPatch):
    """Records every mutation, in order, with its dry_run flag."""
    calls: list[tuple] = []
    state = {"droplet": DROPLET, "delete_rc": 0, "volume": dict(VOLUME), "firewall": None}

    monkeypatch.setattr("catalpa_tooling.deploy_do_link.find_droplet_for_link", lambda *a, **k: state["droplet"])
    monkeypatch.setattr("catalpa_tooling.deploy_do_link._orphan_droplet_message", lambda *a, **k: state.get("orphan"))
    monkeypatch.setattr("catalpa_tooling.doctl_block_storage.find_volume_by_name",
                        lambda name, **k: state["volume"] if name == "fcos-data" else None)  # fmt: skip
    target = HostDnsTarget(hostname="staging.example.org", zone="example.org", record_name="staging")
    monkeypatch.setattr("catalpa_tooling.doctl_domains.host_dns_records_at",
                        lambda info, **k: ([(target, {"id": 9, "data": IP})], []))  # fmt: skip

    def rec(name, rc=0):
        def f(*a, dry_run=False, **k):
            calls.append((name, a, dry_run))
            return state["delete_rc"] if name == "delete_droplet" else rc

        return f

    monkeypatch.setattr("catalpa_tooling.doctl_domains.remove_host_dns", rec("remove_dns"))
    monkeypatch.setattr("catalpa_tooling.doctl_droplets.shutdown_droplet", rec("shutdown"))
    monkeypatch.setattr("catalpa_tooling.doctl_droplets.delete_droplet", rec("delete_droplet"))
    monkeypatch.setattr("catalpa_tooling.doctl_block_storage.detach_volume_from_droplet", rec("detach"))
    monkeypatch.setattr("catalpa_tooling.doctl_block_storage.delete_volume", rec("delete_volume"))
    monkeypatch.setattr("catalpa_tooling.ssh_known_hosts.remove_known_host", rec("known_hosts"))

    def find_firewall(name, **k):
        if state["firewall"] == "forbidden":
            from catalpa_tooling.doctl_binary import DoctlCommandError

            raise DoctlCommandError("403", returncode=1)
        return state["firewall"]

    monkeypatch.setattr("catalpa_tooling.doctl_firewall.find_firewall", find_firewall)
    monkeypatch.setattr("catalpa_tooling.doctl_firewall.delete_firewall", rec("delete_firewall"))
    return calls, state


def names(calls) -> list[str]:
    return [c[0] for c in calls]


def test_refused_without_allow_destroy_even_with_yes(env, doctl, capsys) -> None:
    config, _ = env(extra="")
    assert hd.cmd_env_host_destroy(config, "staging", ["--yes"]) == 1
    assert doctl[0] == []
    assert "allow_destroy: true" in capsys.readouterr().err


def test_refused_when_digitalocean_is_disabled(env, doctl) -> None:
    config, _ = env(extra="  disabled: true\n  allow_destroy: true\n")
    assert hd.cmd_env_host_destroy(config, "staging", ["--yes"]) == 1
    assert doctl[0] == []


def test_refused_without_a_tty_or_yes(env, doctl, monkeypatch) -> None:
    config, _ = env()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert hd.cmd_env_host_destroy(config, "staging", []) == 1
    assert doctl[0] == []


def test_typed_confirmation_must_match(env, doctl, monkeypatch) -> None:
    config, _ = env()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("catalpa_tooling.cli_confirm.confirm_by_typing_env_name", lambda e: False)
    assert hd.cmd_env_host_destroy(config, "staging", []) == 1
    assert doctl[0] == []


def test_happy_path_in_order(env, doctl, capsys) -> None:
    config, info = env()
    assert hd.cmd_env_host_destroy(config, "staging", ["--yes"]) == 0
    calls, _ = doctl
    assert names(calls) == ["remove_dns", "shutdown", "detach", "delete_droplet", "delete_volume",
                            "known_hosts"]  # fmt: skip
    assert all(dry is False for _n, _a, dry in calls)
    assert ("delete_droplet", ("42",), False) in calls
    assert ("delete_volume", ("vol-1",), False) in calls
    text = info.read_text()
    assert "docker_host" not in text and "# a comment that must survive" in text
    err = capsys.readouterr().err
    assert "No backup is taken" in err and "fcos-data (10 GiB)" in err


def test_docker_host_by_hostname_is_kept(env, doctl) -> None:
    """A hostname stays right for the next host behind it; both known_hosts entries go."""
    config, info = env(host="staging.example.org")
    assert hd.cmd_env_host_destroy(config, "staging", ["--yes"]) == 0
    assert "docker_host: ssh://deploy@staging.example.org" in info.read_text()
    forgotten = [a[0] for n, a, _d in doctl[0] if n == "known_hosts"]
    assert forgotten == [IP, "staging.example.org"]


def test_a_quadlet_env_volume_is_its_data_volume(env) -> None:
    config, _ = env()
    info = {"runtime": "quadlet", "fcos": {"hostname": "fcos"}}
    assert hd.env_volume_names(config, "staging", info, "x") == ["fcos-data"]
    info["storage"] = {"data_volume": {"digitalocean": {"name": "other-data", "size_gib": 20}}}
    assert hd.env_volume_names(config, "staging", info, "x") == ["other-data"]
    info["storage"] = {"data_volume": {"path": "/var/mnt/data"}}  # no DO volume
    assert hd.env_volume_names(config, "staging", info, "x") == []


@pytest.mark.parametrize(
    "flag,skipped",
    [("--keep-dns", {"remove_dns"}), ("--keep-volumes", {"detach", "delete_volume"}),
     ("--keep-docker-host", {"known_hosts"})],
)  # fmt: skip
def test_each_keep_flag_skips_its_step(env, doctl, flag, skipped) -> None:
    config, info = env()
    before = info.read_text()
    assert hd.cmd_env_host_destroy(config, "staging", ["--yes", flag]) == 0
    all_steps = {"remove_dns", "shutdown", "detach", "delete_droplet", "delete_volume", "known_hosts"}
    assert set(names(doctl[0])) == all_steps - skipped
    if flag == "--keep-docker-host":
        assert info.read_text() == before


def test_a_failed_droplet_delete_stops_before_the_volumes(env, doctl) -> None:
    config, info = env()
    doctl[1]["delete_rc"] = 3
    before = info.read_text()
    assert hd.cmd_env_host_destroy(config, "staging", ["--yes"]) == 3
    assert "delete_volume" not in names(doctl[0]) and "known_hosts" not in names(doctl[0])
    assert info.read_text() == before


def test_gone_droplet_is_idempotent_and_an_orphan_is_refused(env, doctl, capsys) -> None:
    config, _ = env()
    doctl[1]["droplet"] = None
    doctl[1]["volume"] = None
    assert hd.cmd_env_host_destroy(config, "staging", ["--yes"]) == 0
    assert doctl[0] == []
    doctl[1]["orphan"] = "Droplet 'x-staging' exists on this account … not in project"
    assert hd.cmd_env_host_destroy(config, "staging", ["--yes"]) == 1
    assert "not in project" in capsys.readouterr().err


@pytest.mark.parametrize("argv,global_dry", [(["--dry-run"], False), ([], True)])
def test_dry_run_changes_nothing_and_asks_nothing(env, doctl, capsys, argv, global_dry) -> None:
    """Both spellings. The confirm helpers aren't patched: reaching them would hang the test."""
    config, info = env()
    before = info.read_text()
    assert hd.cmd_env_host_destroy(config, "staging", argv, global_dry_run=global_dry) == 0
    assert doctl[0] == []
    assert info.read_text() == before
    err = capsys.readouterr().err
    assert err.count("dry-run") == 1 and "would run" not in err


def test_clear_info_docker_host_keeps_comments(tmp_path) -> None:
    p = tmp_path / "info.yaml"
    p.write_text("# top\nname: x\ndocker_host: ssh://root@1.2.3.4\n# end\n")
    assert hd.clear_info_docker_host(p) == 0
    assert p.read_text() == "# top\nname: x\n# end\n"


def test_remove_host_dns_only_deletes_records_at_the_ip(monkeypatch) -> None:
    from catalpa_tooling import doctl_domains as dd

    monkeypatch.setattr(dd, "list_registered_domains", lambda **k: ["example.org"])
    records = {"a": {"id": 1, "data": IP}, "b": {"id": 2, "data": "198.51.100.1"}}
    monkeypatch.setattr(dd, "find_a_record_exact", lambda zone, name, **k: records.get(name))
    deleted = []
    monkeypatch.setattr(dd, "_delete_domain_record", lambda zone, rid, **k: deleted.append(rid))
    info = {"site_origin": ["a.example.org", "b.example.org"]}
    assert dd.remove_host_dns(info, droplet_ip=IP, context=None) == 0
    assert deleted == [1]


def test_dk_parses_and_routes_host_destroy(env, monkeypatch) -> None:
    """`dk staging host destroy --dry-run` and `dk staging --dry-run host destroy` reach the
    command the same way; unknown flags are still errors."""
    from catalpa_tooling.cli.dk_argv import normalize_dk_env_argv
    from catalpa_tooling.dk_cli import _parse_env_argv
    from catalpa_tooling.dk_parser import build_dk_parser
    from catalpa_tooling.env_handlers import handle_env_command

    config, _ = env()
    seen = []
    monkeypatch.setattr(hd, "cmd_env_host_destroy", lambda c, e, argv, **k: seen.append((argv, k)) or 0)
    for argv in (["staging", "host", "destroy", "--dry-run"], ["staging", "--dry-run", "host", "destroy"]):
        ns = _parse_env_argv(build_dk_parser(config), normalize_dk_env_argv(argv))
        assert handle_env_command(ns, config) == 0
    assert seen[0][0] == ["--dry-run"] and seen[1] == ([], {"global_dry_run": True, "global_yes": False})
    with pytest.raises(SystemExit):
        hd._parser("staging").parse_args(["--nope"])


def test_a_leftover_volume_is_deleted_once_the_droplet_is_gone(env, doctl, monkeypatch, capsys) -> None:
    """After `--keep-volumes`, a second destroy finds no droplet but the env's detached volume."""
    config, info = env()
    calls, state = doctl
    state["droplet"] = None
    state["volume"] = {**VOLUME, "droplet_ids": []}
    before = info.read_text()

    assert hd.cmd_env_host_destroy(config, "staging", ["--dry-run"]) == 0
    assert calls == []
    err = capsys.readouterr().err
    assert "fcos-data (10 GiB)  DELETED" in err and err.count("dry-run") == 1

    calls.clear()
    assert hd.cmd_env_host_destroy(config, "staging", ["--keep-volumes", "--yes"]) == 0
    assert calls == []

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("catalpa_tooling.cli_confirm.confirm_by_typing_env_name", lambda e: True)
    monkeypatch.setattr("catalpa_tooling.cli_confirm.confirm_by_typing_expected", lambda *a, **k: False)
    assert hd.cmd_env_host_destroy(config, "staging", []) == 1
    assert calls == []

    assert hd.cmd_env_host_destroy(config, "staging", ["--yes"]) == 0
    assert calls == [("delete_volume", ("vol-1",), False)]
    assert "what was left is deleted" in capsys.readouterr().err
    assert info.read_text() == before


def test_a_volume_attached_to_another_droplet_is_never_touched(env, doctl, capsys) -> None:
    config, _ = env()
    calls, state = doctl
    state["droplet"] = None
    state["volume"] = {**VOLUME, "droplet_ids": [99]}
    assert hd.cmd_env_host_destroy(config, "staging", ["--yes"]) == 1
    assert calls == []
    assert "attached to droplet 99" in capsys.readouterr().err


FIREWALL = {"id": "fw-1", "name": "dk-x-staging", "droplet_ids": [42]}


def test_the_droplets_firewall_goes_after_it(env, doctl, capsys) -> None:
    config, _ = env()
    calls, state = doctl
    state["firewall"] = dict(FIREWALL)
    assert hd.cmd_env_host_destroy(config, "staging", ["--dry-run"]) == 0
    assert "firewall  dk-x-staging  deleted" in capsys.readouterr().err and calls == []
    assert hd.cmd_env_host_destroy(config, "staging", ["--yes"]) == 0
    order = names(calls)
    assert order.index("delete_firewall") > order.index("delete_droplet")
    assert ("delete_firewall", ("fw-1",), False) in calls


@pytest.mark.parametrize(
    "firewall,flag,note",
    [
        ({**FIREWALL, "droplet_ids": [42, 77]}, [], "droplet 77 is in it too; left alone"),
        ("forbidden", [], "lacks firewall:read); left alone"),
        (dict(FIREWALL), ["--keep-firewall"], "kept (--keep-firewall)"),
    ],
)
def test_a_firewall_is_left_when_it_isnt_only_this_droplets(env, doctl, capsys, firewall, flag, note) -> None:
    config, _ = env()
    calls, state = doctl
    state["firewall"] = firewall
    assert hd.cmd_env_host_destroy(config, "staging", ["--yes", *flag]) == 0
    assert "delete_firewall" not in names(calls) and "delete_droplet" in names(calls)
    assert note in capsys.readouterr().err


def test_a_leftover_firewall_goes_once_the_droplet_is_gone(env, doctl, monkeypatch, capsys) -> None:
    """Destroyed before firewalls were removed (or --keep-firewall): a later destroy finds it."""
    config, _ = env()
    calls, state = doctl
    state.update(droplet=None, volume=None, firewall={**FIREWALL, "droplet_ids": []})
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("catalpa_tooling.cli_confirm.confirm_by_typing_env_name", lambda e: True)
    monkeypatch.setattr(  # no volumes, so no delete-volumes question
        "catalpa_tooling.cli_confirm.confirm_by_typing_expected", lambda *a, **k: pytest.fail("asked")
    )
    assert hd.cmd_env_host_destroy(config, "staging", []) == 0
    assert calls == [("delete_firewall", ("fw-1",), False)]
    state["firewall"] = None
    calls.clear()
    assert hd.cmd_env_host_destroy(config, "staging", []) == 0
    assert calls == [] and "Nothing to destroy." in capsys.readouterr().err


@pytest.mark.parametrize(
    "answer,rc,volume_steps",
    [("keep", 0, set()), ("delete-volumes", 0, {"detach", "delete_volume"}), ("", 1, None), ("yes", 1, None)],
)
def test_the_volume_question_can_keep_them_without_starting_again(
    env, doctl, monkeypatch, capsys, answer, rc, volume_steps
) -> None:
    """Typing the env name, then 'keep', goes on as --keep-volumes; anything else but
    'delete-volumes' cancels the whole destroy."""
    config, _ = env()
    calls, _state = doctl
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("catalpa_tooling.cli_confirm.confirm_by_typing_env_name", lambda e: True)
    monkeypatch.setattr("builtins.input", lambda: answer)
    assert hd.cmd_env_host_destroy(config, "staging", []) == rc
    err = capsys.readouterr().err
    assert "'keep' below, keeps it" in err and "'keep' to keep them" in err
    if volume_steps is None:
        assert calls == [] and "cancelled" in err
    else:
        assert "delete_droplet" in names(calls)
        assert {"detach", "delete_volume"} & set(names(calls)) == volume_steps
    if answer == "keep":
        assert "Keeping fcos-data" in err
