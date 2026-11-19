"""``dk <env> fcos render`` / ``check`` (issue #182).

Golden renders of three synthetic projects shaped like catalpa-site, the Bero family and swlt
(``tests/fixtures/fcos/README.md``). Compose is resolved by the real ``docker compose config``;
``caddy adapt`` is faked (it runs the edge image). ``UPDATE_GOLDEN=1`` rewrites ``expected/``.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

import pytest

from catalpa_tooling.config import load_project_config
from catalpa_tooling.fcos.caddy_json import CaddyJsonError, rewrite_listeners
from catalpa_tooling.fcos.quadlets import (
    SECRET_MARK,
    ComposeNotSupported,
    RenderInputs,
    quadlets_from_compose,
    qualify_image,
)
from catalpa_tooling.fcos.render import (
    RenderError,
    compare_render,
    render_env,
    required_secret_problems,
    write_render,
)
from catalpa_tooling.fcos.settings import FcosConfigError, FcosSettings, parse_fcos_settings

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fcos"
needs_compose = pytest.mark.skipif(
    shutil.which("docker") is None, reason="needs the docker CLI (docker compose config)"
)


def _fake_adapter(entrypoint: list[str] | None = None, *, calls: list | None = None):
    def adapt(image, env, caddyfile, platform):
        if calls is not None:
            calls.append((image, dict(env), caddyfile, platform))
        site = env.get("CADDY_SITE_ADDRESS", "localhost")
        return (
            {
                "apps": {
                    "http": {
                        "servers": {
                            "srv0": {
                                "listen": [":443"],
                                "routes": [
                                    {
                                        "match": [{"host": [site]}],
                                        "handle": [
                                            {
                                                "handler": "reverse_proxy",
                                                "upstreams": [{"dial": "django:8000"}],
                                            }
                                        ],
                                    }
                                ],
                            }
                        }
                    }
                }
            },
            list(entrypoint or []),
        )

    return adapt


_ENTRYPOINTS = {"site": [], "bero": [], "swlt": ["/entrypoint.sh"]}


def _render(name: str, **kwargs):
    config = load_project_config(FIXTURES / name)
    return config, render_env(
        config, "staging", adapter=kwargs.pop("adapter", _fake_adapter(_ENTRYPOINTS[name]))
    )


@needs_compose
@pytest.mark.parametrize("name", ["site", "bero", "swlt"])
def test_golden_render(name: str) -> None:
    _config, render = _render(name)
    expected = FIXTURES / name / "expected"
    if os.environ.get("UPDATE_GOLDEN"):
        write_render(expected, render.files)
    assert compare_render(expected, render.files) == []


@needs_compose
def test_render_holds_no_secret_value_and_names_secrets() -> None:
    _config, render = _render("bero")
    text = "".join(render.files.values())
    assert "not-a-secret" not in text
    assert "@@dk-secret" not in text
    django = render.files["quadlets/django.container"]
    assert "Secret=django_secret_key,type=env,target=DJANGO_SECRET_KEY" in django
    # Credentials never reach a settings file, which `podman inspect` shows.
    assert "DJANGO_SECRET_KEY" not in render.files["env/django.env"]


@needs_compose
def test_render_is_reproducible() -> None:
    _config, first = _render("site")
    _config, second = _render("site")
    assert first.files == second.files


@needs_compose
@pytest.mark.parametrize(
    ("name", "flagged"),
    [("site", []), ("bero", []), ("swlt", ["SITE_AUTH"])],
)
def test_required_secret_check(name: str, flagged: list[str]) -> None:
    config, render = _render(name)
    problems = required_secret_problems(config, render)
    assert [p.split(":", 1)[0] for p in problems] == flagged


@needs_compose
def test_bero_unused_credential_is_not_checked() -> None:
    _config, render = _render("bero")
    # pgbr_s3_write_key is for backups, not the compose file.
    assert "PGBR_S3_WRITE_KEY" not in render.secret_keys
    assert {"POSTGRES_PASSWORD", "DJANGO_SECRET_KEY", "METABASE_DB_PASSWORD"} <= render.secret_keys


@needs_compose
def test_adapter_gets_markers_and_a_marker_in_caddy_json_is_refused() -> None:
    calls: list = []
    _render("swlt", adapter=_fake_adapter(["/entrypoint.sh"], calls=calls))
    ((image, env, caddyfile, _platform),) = calls
    assert image == "ghcr.io/example/swlt-caddy:v0.16.1"
    assert env["SITE_AUTH"] == SECRET_MARK.format(key="SITE_AUTH")
    assert caddyfile == "/etc/caddy/Caddyfile"

    def leaky(image, env, caddyfile, platform):
        cfg, ep = _fake_adapter()(image, env, caddyfile, platform)
        cfg["apps"]["http"]["servers"]["srv0"]["routes"][0]["match"][0]["host"] = [env["SITE_AUTH"]]
        return cfg, ep

    config = load_project_config(FIXTURES / "swlt")
    with pytest.raises(RenderError, match="uses a credential at adapt time"):
        render_env(config, "staging", adapter=leaky)


@needs_compose
def test_write_render_removes_stale_files_and_previous_ranges_stick(tmp_path: Path) -> None:
    src = FIXTURES / "site"
    root = tmp_path / "site"
    shutil.copytree(src, root, ignore=shutil.ignore_patterns("expected"))
    config = load_project_config(root)
    gen = root / "docker" / "envs" / "staging" / "generated" / "fcos"
    (gen / "quadlets").mkdir(parents=True)
    (gen / "quadlets" / "old.container").write_text("x")
    render = render_env(config, "staging", adapter=_fake_adapter())
    changed = write_render(gen, render.files)
    assert "- quadlets/old.container" in changed
    assert not (gen / "quadlets" / "old.container").exists()
    assert compare_render(gen, render.files) == []

    # Unpin the ranges: the committed manifest keeps each group where it was.
    info = root / "docker" / "envs" / "staging" / "info.yaml"
    text = info.read_text().split("  id_ranges:")[0]
    info.write_text(text)
    again = render_env(config, "staging", adapter=_fake_adapter())
    assert again.files["quadlets/db.container"] == render.files["quadlets/db.container"]


@needs_compose
def test_quadlet_runtime_refuses_compose_commands() -> None:
    from catalpa_tooling.env_handlers import handle_env_command

    config = load_project_config(FIXTURES / "site")
    ns = argparse.Namespace(
        env_name="staging",
        env_command=None,
        implicit_compose_argv=["ps"],
        dry_run=False,
        tag=None,
    )
    assert handle_env_command(ns, config) == 2


# --- converter units (no docker) -------------------------------------------------------


def _inputs(**kw) -> RenderInputs:
    base = dict(
        env_name="t",
        project="p",
        compose_rel="compose.yml",
        proxy="caddy",
        settings=FcosSettings(),
        bind_volumes=(),
        config_dir="/etc/p",
    )
    base.update(kw)
    return RenderInputs(**base)


def _resolved(services: dict, volumes: dict | None = None) -> dict:
    services = {"caddy": {"image": "ghcr.io/x/caddy:1", "restart": "always"}, **services}
    return {"name": "p", "services": services, "volumes": volumes or {}}


def test_credential_inside_a_url_is_refused() -> None:
    url = f"postgres://app:{SECRET_MARK.format(key='DB_PASSWORD')}@db/app"
    with pytest.raises(ComposeNotSupported, match="DB_PASSWORD inside a longer value"):
        quadlets_from_compose(
            _resolved({"web": {"image": "ghcr.io/x/web:1", "environment": {"DATABASE_URL": url}}}),
            _inputs(),
        )


def test_ports_on_a_service_other_than_the_edge_are_refused_unless_dropped() -> None:
    svc = {"image": "ghcr.io/x/m:1", "ports": [{"target": 8025, "published": "8025"}]}
    with pytest.raises(ComposeNotSupported, match="only the edge is reachable"):
        quadlets_from_compose(_resolved({"mailpit": svc}), _inputs())
    s = FcosSettings(drop_ports=("mailpit",))
    quadlets_from_compose(_resolved({"mailpit": svc}), _inputs(settings=s))


def test_edge_ports_other_than_80_443_are_refused() -> None:
    resolved = _resolved({})
    resolved["services"]["caddy"]["ports"] = [{"target": 8080, "published": "8080"}]
    with pytest.raises(ComposeNotSupported, match="only 80 and 443"):
        quadlets_from_compose(resolved, _inputs())


def test_bind_mount_and_build_only_service_are_refused() -> None:
    services = {
        "web": {
            "image": "ghcr.io/x/web:1",
            "volumes": [{"type": "bind", "source": "/src", "target": "/app"}],
        },
        "builder": {"build": {"context": "."}},
    }
    with pytest.raises(ComposeNotSupported) as exc:
        quadlets_from_compose(_resolved(services), _inputs())
    assert "the host has no checkout" in str(exc.value)
    assert "service `builder`: has no image" in str(exc.value)


def test_groups_follow_writable_volumes_and_images() -> None:
    def vol(src: str, ro: bool = False) -> dict:
        return {"type": "volume", "source": src, "target": "/d", "read_only": ro}

    services = {
        "django": {"image": "ghcr.io/x/web:1", "volumes": [vol("media")]},
        "worker": {"image": "ghcr.io/x/web:1"},
        "importer": {"image": "ghcr.io/x/imp:1", "volumes": [vol("media")]},
        "redis": {"image": "redis:8"},
    }
    resolved = _resolved(services, {"media": {}})
    resolved["services"]["caddy"]["volumes"] = [vol("media", ro=True)]
    q = quadlets_from_compose(resolved, _inputs(bind_volumes=("media",)))
    ranges = q.manifest["id_ranges"]
    assert ranges["django"]["services"] == ["django", "importer", "worker"]
    assert ranges["caddy"]["services"] == ["caddy"]
    assert q.manifest["bind_volumes"]["media"]["id_range"] == "django"
    # Shared by several containers: the shared SELinux label.
    assert "Volume=/var/mnt/data/media:/d:ro,z" in q.files["quadlets/caddy.container"]
    assert "Image=docker.io/library/redis:8" in q.files["quadlets/redis.container"]


def test_previous_ranges_are_kept_and_overrides_win() -> None:
    services = {"a": {"image": "ghcr.io/x/a:1"}, "b": {"image": "ghcr.io/x/b:1"}}
    q = quadlets_from_compose(
        _resolved(services),
        _inputs(previous_ranges={"b": 50000}, settings=FcosSettings(id_ranges={"a": 40000})),
    )
    starts = {k: v["start"] for k, v in q.manifest["id_ranges"].items()}
    assert starts == {"a": 40000, "b": 50000, "caddy": 10000}


def test_overlapping_pins_are_refused() -> None:
    services = {"a": {"image": "ghcr.io/x/a:1"}}
    with pytest.raises(ComposeNotSupported, match="overlap"):
        quadlets_from_compose(
            _resolved(services),
            _inputs(settings=FcosSettings(id_ranges={"a": 10000, "caddy": 12000})),
        )


def test_qualify_image() -> None:
    assert qualify_image("redis:8") == "docker.io/library/redis:8"
    assert qualify_image("metabase/metabase:v1") == "docker.io/metabase/metabase:v1"
    assert qualify_image("ghcr.io/x/y:1") == "ghcr.io/x/y:1"
    assert qualify_image("localhost/y:1") == "localhost/y:1"


def test_settings_validation() -> None:
    assert parse_fcos_settings({}).user == "deploy"
    with pytest.raises(FcosConfigError, match="unknown fcos keys"):
        parse_fcos_settings({"fcos": {"usr": "x"}})
    with pytest.raises(FcosConfigError, match="unprivileged"):
        parse_fcos_settings({"fcos": {"user": "core"}})
    with pytest.raises(FcosConfigError, match="subordinate IDs"):
        parse_fcos_settings({"fcos": {"id_ranges": {"db": 64000}}})


# --- caddy listeners ---------------------------------------------------------------------


def test_https_only_config_gets_the_redirect_server() -> None:
    cfg = rewrite_listeners({"apps": {"http": {"servers": {"srv0": {"listen": [":443"]}}}}})
    servers = cfg["apps"]["http"]["servers"]
    assert servers["srv0"]["listen"] == ["fd/4", "fdgram/5"]
    assert servers["srv0"]["automatic_https"]["disable_redirects"] is True
    assert servers["fd_http_redirect"]["listen"] == ["fd/3"]


def test_explicit_http_server_uses_fd3_and_no_redirect_is_added() -> None:
    cfg = rewrite_listeners(
        {"apps": {"http": {"servers": {"a": {"listen": [":443"]}, "b": {"listen": [":80"]}}}}}
    )
    servers = cfg["apps"]["http"]["servers"]
    assert servers["b"]["listen"] == ["fd/3"]
    assert "fd_http_redirect" not in servers


def test_other_ports_cannot_be_socket_activated() -> None:
    with pytest.raises(CaddyJsonError, match="only 80 and 443"):
        rewrite_listeners({"apps": {"http": {"servers": {"a": {"listen": [":8443"]}}}}})
