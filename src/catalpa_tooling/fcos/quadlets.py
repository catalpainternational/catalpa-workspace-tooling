"""Rootless Podman quadlets from a resolved compose file.

``quadlets_from_compose`` takes the output of ``docker compose config --format json`` (so
``include``, ``extends``, profiles and ``${VAR}`` are done exactly as compose does them) and
returns the unit files for one FCOS host. Adapted from containerwharf's ``compose_units``
(ADR-0019, ADR-0021); the differences come from running one project per host, as one account:

- each service is ``<service>.container`` with ``ContainerName=<service>``, so containers find
  each other by their compose service names, on one network named as compose names it;
- each group of containers that share files (a writable volume, or the same image) gets its own
  ``UIDMap``/``GIDMap`` range in the account's user namespace, so no container shares a host uid
  with the account or with an unrelated container;
- the volumes that hold data worth keeping are directories on the block storage volume
  (``fcos.data_mount``), the rest are Podman volumes;
- the edge (``stack.services.proxy``) receives 80/443 by socket activation (see ``caddy_json``);
- each credential becomes a Podman secret, never a settings-file line: ``podman inspect`` shows
  settings-file values.

Anything the units cannot do the same way as compose is refused, every key named.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from dataclasses import dataclass, field

from catalpa_tooling.fcos.settings import (
    ID_RANGE_FIRST,
    ID_RANGE_SIZE,
    SUBUID_COUNT,
    SUBUID_START,
    FcosSettings,
)

#: How a credential is written into compose's environment before it resolves, so the resolved
#: file names a secret and never holds its value.
SECRET_MARK = "@@dk-secret:{key}@@"
_SECRET = re.compile(r"@@dk-secret:([A-Za-z_][A-Za-z0-9_]*)@@")

#: Service keys needing nothing in a unit: done by the resolve, only for building, or replaced
#: by the host's network and the edge's sockets.
_IGNORED = {
    "build",
    "platform",
    "networks",
    "ports",
    "expose",
    "container_name",
    "hostname",
    "labels",
    "pull_policy",
    "scale",
    "profiles",
    "extends",
    "env_file",
}
_HANDLED = {
    "image",
    "command",
    "entrypoint",
    "environment",
    "volumes",
    "healthcheck",
    "depends_on",
    "restart",
    "user",
    "working_dir",
    "stop_grace_period",
    "stop_signal",
    "tmpfs",
    "shm_size",
    "mem_limit",
    "deploy",
    "logging",
    "init",
    "cap_add",
    "cap_drop",
    "read_only",
}
_RESTART = {"no": "no", "always": "always", "unless-stopped": "always", "on-failure": "on-failure"}
_LOCAL_LOG_DRIVERS = {"json-file", "local", "journald"}
_EDGE_PORTS = {80, 443}


class ComposeNotSupported(ValueError):
    """The compose file asks for something the units cannot do the same way."""


@dataclass(frozen=True)
class QuadletRender:
    files: dict[str, str]
    """Path under ``generated/fcos/`` → text (everything but the edge's caddy.json)."""
    manifest: dict
    proxy_env: dict[str, str]
    """The edge's settings, for ``caddy adapt``."""
    proxy_image: str
    proxy_platform: str | None
    install: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class RenderInputs:
    env_name: str
    project: str
    """tooling.yaml ``project.name`` (``/etc/<project>``, ``~/.config/<project>``)."""
    compose_rel: str
    proxy: str
    settings: FcosSettings
    bind_volumes: tuple[str, ...]
    config_dir: str
    proxy_wrapped: bool = False
    """The edge's entrypoint (compose's or the image's) wraps Caddy and takes its arguments."""
    previous_ranges: dict[str, int] = field(default_factory=dict)
    """Service → range start in the committed manifest, kept so that adding a service never
    moves an existing group's range (its data is owned by host uids from that range)."""


def has_secret(text: str) -> bool:
    return bool(_SECRET.search(text))


def secret_name(key: str) -> str:
    """The Podman secret for a credential: ``POSTGRES_PASSWORD`` → ``postgres_password``."""
    return key.lower()


def qualify_image(image: str) -> str:
    """FCOS refuses short names: ``redis:7`` → ``docker.io/library/redis:7``."""
    first, _, rest = image.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        return image
    if not rest:
        return f"docker.io/library/{image}"
    return f"docker.io/{image}"


def quadlets_from_compose(resolved: dict, inputs: RenderInputs) -> QuadletRender:
    resolved = _unescape_dollars(resolved)
    s = inputs.settings
    problems = [
        f"top-level `{key}` is not supported"
        for key in sorted(set(resolved) - {"name", "services", "volumes", "networks"})
        if not key.startswith("x-")
    ]
    network_name, subnet = _network_of(resolved, problems)
    services = {
        name: svc
        for name, svc in sorted((resolved.get("services") or {}).items())
        if svc.get("scale", 1) != 0 and (svc.get("deploy") or {}).get("replicas", 1) != 0
    }
    if inputs.proxy not in services:
        problems.append(f"no service `{inputs.proxy}` for the edge (tooling.yaml stack.services.proxy)")
    top_volumes = resolved.get("volumes") or {}
    unknown_binds = sorted(set(inputs.bind_volumes) - set(top_volumes))
    problems.extend(
        f"fcos.bind_volumes: `{v}` is not a volume of the compose file" for v in unknown_binds
    )

    images: dict[str, str] = {}
    for name, svc in services.items():
        image = svc.get("image")
        if not image:
            problems.append(f"service `{name}`: has no image; the host pulls, never builds")
        elif _SECRET.search(image):
            problems.append(f"service `{name}`: its image name uses a credential")
        else:
            images[name] = qualify_image(str(image))

    mounts = {name: _named_mounts(svc) for name, svc in services.items()}
    groups = _groups(services, images, mounts)
    labels = {tuple(g): _group_label(g, inputs.proxy) for g in groups}
    starts = _assign_ranges(groups, labels, s.id_ranges, inputs.previous_ranges, problems)
    group_of = {svc: tuple(g) for g in groups for svc in g}

    healthy_wanted = {
        dep
        for svc in services.values()
        for dep, how in _depends_on(svc).items()
        if how.get("condition") == "service_healthy"
    }
    one_off_for: dict[str, list[str]] = {}
    for name, svc in services.items():
        for dep, how in _depends_on(svc).items():
            if how.get("condition") == "service_completed_successfully":
                one_off_for.setdefault(dep, []).append(name)

    users_of_volume: dict[str, set[str]] = {}
    for name, ms in mounts.items():
        for source, _ro in ms:
            users_of_volume.setdefault(source, set()).add(name)

    header = (
        f"# Generated by `dk {inputs.env_name} fcos render` from {inputs.compose_rel}. "
        "Do not edit: change the compose file or info.yaml and render again.\n"
    )
    files: dict[str, str] = {}
    install: list[dict] = []
    secrets: dict[str, str] = {}
    proxy_env: dict[str, str] = {}

    def add(path: str, text: str, dest: str, owner: str) -> None:
        files[path] = text
        install.append({"file": path, "dest": dest, "owner": owner, "mode": "0644"})

    net_file = f"{network_name}.network"
    for name, svc in services.items():
        where = f"service `{name}`"
        problems.extend(
            f"{where}: `{key}` is not supported" for key in sorted(set(svc) - _IGNORED - _HANDLED)
        )
        _check_networks(svc, where, problems)
        is_proxy = name == inputs.proxy
        _check_ports(svc, where, is_proxy, name in s.drop_ports, problems)

        env_lines, secret_lines = _environment(svc, where, problems, secrets)
        problems.extend(
            f"{where}: `{key}` uses a credential; only an environment variable can be handed "
            "one, as its whole value"
            for key, value in sorted(svc.items())
            if key not in ("environment", "image") and _SECRET.search(json.dumps(value))
        )
        if is_proxy:
            proxy_env = {k: v for k, v in (svc.get("environment") or {}).items() if v is not None}
            proxy_env = {k: str(v) for k, v in proxy_env.items()}
            if svc.get("command"):
                problems.append(
                    f"{where}: `command` is set; the edge runs Caddy on the generated caddy.json"
                )

        group = group_of.get(name, (name,))
        start = starts.get(group)
        text = _container(
            header=header,
            project=inputs.project,
            name=name,
            svc=svc,
            image=images.get(name, ""),
            net_file=net_file,
            settings=s,
            config_dir=inputs.config_dir,
            start=start,
            env_lines=env_lines,
            secret_lines=secret_lines,
            bind_volumes=inputs.bind_volumes,
            top_volumes=top_volumes,
            users_of_volume=users_of_volume,
            healthy_wanted=name in healthy_wanted,
            one_off_for=one_off_for.get(name, []),
            is_proxy=is_proxy,
            proxy_wrapped=inputs.proxy_wrapped,
            services=services,
            where=where,
            problems=problems,
        )
        add(f"quadlets/{name}.container", text, f"{s.quadlet_dir}/{name}.container", "root")
        add(
            f"env/{name}.env",
            f"# Settings for {name}; credentials are Podman secrets (see {name}.container).\n"
            + "".join(f"{line}\n" for line in env_lines),
            f"{inputs.config_dir}/{name}.env",
            "root",
        )

    if problems:
        raise ComposeNotSupported(
            "the compose file cannot run as quadlets as it is:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    add(
        f"quadlets/{net_file}",
        header
        + "# compose's default network: containers find each other by their service names.\n"
        "[Network]\n"
        f"NetworkName={network_name}\n" + (f"Subnet={subnet}\n" if subnet else ""),
        f"{s.quadlet_dir}/{net_file}",
        "root",
    )
    podman_volumes = sorted(
        v for v in users_of_volume if v not in inputs.bind_volumes and v in top_volumes
    )
    for v in podman_volumes:
        add(
            f"quadlets/{v}.volume",
            header + "[Volume]\n" + f"VolumeName={_volume_name(v, top_volumes)}\n",
            f"{s.quadlet_dir}/{v}.volume",
            "root",
        )

    from catalpa_tooling.fcos.caddy_json import socket_unit

    add(
        f"systemd/{inputs.proxy}.socket",
        header + socket_unit(f"{inputs.project} edge sockets (80, 443, 443/udp)"),
        f"{s.user_unit_dir}/{inputs.proxy}.socket",
        s.user,
    )
    install.append(
        {
            "file": "caddy/caddy.json",
            "dest": f"{s.home}/.config/{inputs.project}/caddy.json",
            "owner": s.user,
            "mode": "0644",
        }
    )

    id_ranges = {}
    for g in groups:
        key = tuple(g)
        if key not in starts:
            continue
        id_ranges[labels[key]] = {
            "start": starts[key],
            "size": ID_RANGE_SIZE,
            "host_root_uid": SUBUID_START + starts[key] - 1,
            "services": list(g),
        }
    manifest = {
        "env": inputs.env_name,
        "compose_file": inputs.compose_rel,
        "user": s.user,
        "uid": s.uid,
        "subuid": {"start": SUBUID_START, "count": SUBUID_COUNT},
        "auto_update": s.auto_update,
        "network": network_name,
        "id_ranges": dict(sorted(id_ranges.items())),
        "bind_volumes": {
            v: {
                "path": f"{s.data_mount}/{v}",
                "services": sorted(users_of_volume.get(v, ())),
                # The range whose root owns the directory: its writers share one (see _groups).
                "id_range": labels[group_of[writers[0]]] if (writers := _writers(v, mounts)) else None,
            }
            for v in sorted(inputs.bind_volumes)
        },
        "podman_volumes": {v: _volume_name(v, top_volumes) for v in podman_volumes},
        "images": dict(sorted(images.items())),
        "secrets": dict(sorted(secrets.items())),
        "install": sorted(install, key=lambda i: i["file"]),
    }
    return QuadletRender(
        files=files,
        manifest=manifest,
        proxy_env=proxy_env,
        proxy_image=images.get(inputs.proxy, ""),
        proxy_platform=(services.get(inputs.proxy) or {}).get("platform"),
        install=manifest["install"],
    )


# --- ID ranges ---------------------------------------------------------------------------


def _writers(volume: str, mounts: dict[str, list[tuple[str, bool]]]) -> list[str]:
    return sorted(name for name, ms in mounts.items() if (volume, False) in ms)


def _named_mounts(svc: dict) -> list[tuple[str, bool]]:
    """(volume, read_only) for each named volume the service mounts."""
    out = []
    for vol in svc.get("volumes") or []:
        if isinstance(vol, dict) and vol.get("type") == "volume" and vol.get("source"):
            out.append((str(vol["source"]), bool(vol.get("read_only"))))
    return out


def _groups(
    services: dict[str, dict], images: dict[str, str], mounts: dict[str, list[tuple[str, bool]]]
) -> list[list[str]]:
    """Services that write the same files share an ID range: those mounting one volume
    writable, and those running one image (migrate and collectstatic run django's)."""
    parent = {s: s for s in services}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_key: dict[str, str] = {}
    for name in services:
        keys = [f"image:{images[name]}"] if name in images else []
        keys += [f"volume:{src}" for src, ro in mounts[name] if not ro]
        for key in keys:
            if key in by_key:
                union(by_key[key], name)
            else:
                by_key[key] = name
    out: dict[str, list[str]] = {}
    for name in services:
        out.setdefault(find(name), []).append(name)
    return sorted((sorted(g) for g in out.values()), key=lambda g: g[0])


def _group_label(group: list[str], proxy: str) -> str:
    # The service the others exist for names the group: django, not collectstatic.
    for preferred in ("django", "web", proxy):
        if preferred in group:
            return preferred
    return group[0]


def _assign_ranges(
    groups: list[list[str]],
    labels: dict[tuple[str, ...], str],
    overrides: dict[str, int],
    previous: dict[str, int],
    problems: list[str],
) -> dict[tuple[str, ...], int]:
    starts: dict[tuple[str, ...], int] = {}
    members = {svc for g in groups for svc in g}
    problems.extend(
        f"fcos.id_ranges: `{svc}` is not a running service" for svc in sorted(set(overrides) - members)
    )
    for g in groups:
        pinned = sorted({overrides[svc] for svc in g if svc in overrides})
        if len(pinned) > 1:
            problems.append(
                f"fcos.id_ranges pins {', '.join(map(str, pinned))} for one group "
                f"({', '.join(g)}), which shares files and so must share a range"
            )
        if pinned:
            starts[tuple(g)] = pinned[0]
    for g in groups:
        key = tuple(g)
        if key in starts:
            continue
        before = sorted({previous[svc] for svc in g if svc in previous})
        if before and not _overlaps(before[0], starts.values()):
            starts[key] = before[0]
    for g in groups:
        key = tuple(g)
        if key in starts:
            continue
        candidate = ID_RANGE_FIRST
        while _overlaps(candidate, starts.values()):
            candidate += ID_RANGE_SIZE
        if candidate + ID_RANGE_SIZE - 1 > SUBUID_COUNT:
            problems.append(
                f"no room for another {ID_RANGE_SIZE}-ID range for {labels[key]}; "
                "pin ranges with fcos.id_ranges"
            )
            continue
        starts[key] = candidate
    pinned_list = sorted(starts.items(), key=lambda kv: kv[1])
    for (ga, a), (gb, b) in itertools.pairwise(pinned_list):
        if a + ID_RANGE_SIZE > b:
            problems.append(
                f"ID ranges of {labels[ga]} ({a}) and {labels[gb]} ({b}) overlap; "
                f"each takes {ID_RANGE_SIZE} IDs"
            )
    return starts


def _overlaps(start: int, taken) -> bool:
    return any(abs(start - other) < ID_RANGE_SIZE for other in taken)


# --- one service -------------------------------------------------------------------------


def _network_of(resolved: dict, problems: list[str]) -> tuple[str, str | None]:
    nets = resolved.get("networks") or {}
    for name in sorted(set(nets) - {"default"}):
        problems.append(f"network `{name}`: only compose's default network is supported")
    net = nets.get("default") or {}
    name = str(net.get("name") or f"{resolved.get('name', 'app')}_default")
    ipam = net.get("ipam") or {}
    config = ipam.get("config") or []
    if not config:
        return name, None
    if len(config) != 1 or set(config[0]) - {"subnet"} or set(ipam) - {"config"}:
        problems.append("network `default`: only one pinned `subnet` is supported")
        return name, None
    return name, str(config[0]["subnet"])


def _check_networks(svc: dict, where: str, problems: list[str]) -> None:
    nets = svc.get("networks") or {}
    for name, how in nets.items():
        if name != "default":
            problems.append(f"{where}: joins network `{name}`; only the default is supported")
        elif how:
            problems.append(f"{where}: network settings ({', '.join(sorted(how))}) are not supported")


def _check_ports(svc: dict, where: str, is_proxy: bool, dropped: bool, problems: list[str]) -> None:
    ports = svc.get("ports") or []
    if not ports or dropped:
        return
    if not is_proxy:
        problems.append(
            f"{where}: publishes ports; on FCOS only the edge is reachable (list the service in "
            "fcos.drop_ports to drop them)"
        )
        return
    for p in ports:
        target = int(p.get("target", 0)) if isinstance(p, dict) else 0
        published = str(p.get("published", "")) if isinstance(p, dict) else ""
        if target not in _EDGE_PORTS or published not in ("", str(target)):
            problems.append(
                f"{where}: publishes {published or '?'}:{target}; the edge gets only 80 and 443, "
                "from its sockets"
            )


def _environment(
    svc: dict, where: str, problems: list[str], secrets: dict[str, str]
) -> tuple[list[str], list[str]]:
    env_lines: list[str] = []
    secret_lines: list[str] = []
    for key, value in sorted((svc.get("environment") or {}).items()):
        if value is None:
            continue
        value = str(value)
        whole = _SECRET.fullmatch(value)
        if whole:
            cred = whole.group(1)
            name = secret_name(cred)
            secrets[name] = cred
            secret_lines.append(f"Secret={name},type=env,target={key}")
        elif _SECRET.search(value):
            creds = ", ".join(sorted(set(_SECRET.findall(value))))
            problems.append(
                f"{where}: {key} has the credential {creds} inside a longer value (a URL?); "
                "only a whole value can be a secret, so pass it as its own variable"
            )
        elif "\n" in value:
            problems.append(f"{where}: {key} spans several lines, which a settings file cannot hold")
        else:
            env_lines.append(f"{key}={value}")
    return env_lines, secret_lines


def _volume_name(key: str, top_volumes: dict) -> str:
    spec = top_volumes.get(key) or {}
    return str(spec.get("name") or key)


def _container(
    *,
    header: str,
    project: str,
    name: str,
    svc: dict,
    image: str,
    net_file: str,
    settings: FcosSettings,
    config_dir: str,
    start: int | None,
    env_lines: list[str],
    secret_lines: list[str],
    bind_volumes: tuple[str, ...],
    top_volumes: dict,
    users_of_volume: dict[str, set[str]],
    healthy_wanted: bool,
    one_off_for: list[str],
    is_proxy: bool,
    proxy_wrapped: bool,
    services: dict[str, dict],
    where: str,
    problems: list[str],
) -> str:
    unit: list[str] = [f"Description={project} {name}"]
    requires: list[str] = []
    wants: list[str] = []
    for dep, how in sorted(_depends_on(svc).items()):
        if dep not in services:
            problems.append(f"{where}: depends on `{dep}`, which does not run")
            continue
        condition = how.get("condition", "service_started")
        if condition == "service_healthy" and not _healthcheck(services[dep]):
            problems.append(f"{where}: waits for `{dep}` to be healthy, but it has no healthcheck")
        if condition in ("service_healthy", "service_completed_successfully") and how.get(
            "required", True
        ):
            requires.append(f"{dep}.service")
        else:
            wants.append(f"{dep}.service")
    if is_proxy:
        requires.append(f"{name}.socket")
    if requires:
        unit += [f"Requires={' '.join(requires)}"]
    if wants:
        unit += [f"Wants={' '.join(wants)}"]
    if requires or wants:
        unit += [f"After={' '.join(requires + wants)}"]
    if one_off_for:
        # Runs again whenever what waits for it restarts (a new image from a deploy or
        # auto-update), as `docker compose up` runs it again.
        unit += [f"PartOf={' '.join(f'{s}.service' for s in sorted(one_off_for))}"]

    volume_lines: list[str] = []
    uses_data_mount = False
    for vol in svc.get("volumes") or []:
        line, on_data = _volume(vol, settings, bind_volumes, top_volumes, users_of_volume, where, problems)
        if line:
            volume_lines.append(line)
            uses_data_mount = uses_data_mount or on_data
    if is_proxy:
        volume_lines.append(
            f"Volume={settings.home}/.config/{project}/caddy.json:/etc/caddy/caddy.json:ro,Z"
        )
    if uses_data_mount:
        unit.append(f"RequiresMountsFor={settings.data_mount}")

    c = [
        f"ContainerName={name}",
        f"Image={image}",
    ]
    if settings.auto_update and not one_off_for:
        c.append("AutoUpdate=registry")
    c.append(f"Network={net_file}")
    if start is not None:
        c += [f"UIDMap=0:{start}:{ID_RANGE_SIZE}", f"GIDMap=0:{start}:{ID_RANGE_SIZE}"]
    c.append(f"EnvironmentFile={config_dir}/{name}.env")
    c += secret_lines
    c += volume_lines
    for path in _tmpfs(svc):
        c.append(f"Tmpfs={_exec_escape(path)}")
    if svc.get("shm_size"):
        c.append(f"ShmSize={svc['shm_size']}")
    for part in ("entrypoint", "command"):
        if svc.get(part) is not None and not svc[part]:
            problems.append(f"{where}: an empty `{part}` (resetting the image's) is not supported")
        if any("\n" in str(w) for w in _words(svc.get(part))):
            problems.append(f"{where}: `{part}` has a line break, which a unit cannot hold")
    if svc.get("entrypoint"):
        ep = _words(svc["entrypoint"])
        c.append(f"Entrypoint={_exec_escape(ep[0] if len(ep) == 1 else json.dumps(ep))}")
    if is_proxy:
        # An image whose entrypoint wraps Caddy (swlt's derives a token from a secret, then
        # `exec caddy "$@"`) keeps it and gets Caddy's arguments; otherwise run Caddy itself.
        args = "run --config /etc/caddy/caddy.json"
        c.append(f"Exec={args}" if proxy_wrapped else f"Exec=caddy {args}")
    elif svc.get("command"):
        c.append(f"Exec={_exec_words(_words(svc['command']))}")
    if svc.get("user"):
        c.append(f"User={_exec_escape(str(svc['user']))}")
    if svc.get("working_dir"):
        c.append(f"WorkingDir={_exec_escape(svc['working_dir'])}")
    if svc.get("init"):
        c.append("RunInit=true")
    if svc.get("read_only"):
        c.append("ReadOnly=true")
    for cap in svc.get("cap_add") or []:
        c.append(f"AddCapability={cap}")
    for cap in svc.get("cap_drop") or []:
        c.append(f"DropCapability={cap}")
    if svc.get("stop_signal"):
        c.append(f"StopSignal={svc['stop_signal']}")
    if svc.get("stop_grace_period") not in (None, ""):
        grace = _seconds(svc["stop_grace_period"])
        if grace is None:
            problems.append(f"{where}: stop_grace_period {svc['stop_grace_period']!r} is not a duration")
        else:
            c.append(f"StopTimeout={grace}")
    c += _health_lines(svc, healthy_wanted, where, problems)
    c += _limits(svc, where, problems)
    driver = (svc.get("logging") or {}).get("driver")
    if driver is not None and driver not in _LOCAL_LOG_DRIVERS:
        problems.append(f"{where}: logging driver {driver!r} is not supported")

    restart = str(svc.get("restart") or "no")
    service: list[str]
    install: list[str] = []
    if one_off_for:
        # What waits for it waits until it has finished, as
        # `condition: service_completed_successfully` does.
        service = ["Type=oneshot", "RemainAfterExit=yes", "TimeoutStartSec=1800"]
    elif restart not in _RESTART:
        problems.append(f"{where}: restart {restart!r} is not supported")
        service = []
    else:
        # Covers the image pull on first boot.
        service = [f"Restart={_RESTART[restart]}", "TimeoutStartSec=900"]
        install = ["", "[Install]", "WantedBy=default.target"]

    lines = [header.rstrip("\n"), "[Unit]", *unit, "", "[Container]", *c, "", "[Service]", *service]
    return "\n".join(lines + install) + "\n"


def _volume(
    vol,
    settings: FcosSettings,
    bind_volumes: tuple[str, ...],
    top_volumes: dict,
    users_of_volume: dict[str, set[str]],
    where: str,
    problems: list[str],
) -> tuple[str | None, bool]:
    if not isinstance(vol, dict) or vol.get("type") != "volume" or not vol.get("source"):
        kind, target = (vol.get("type"), vol.get("target")) if isinstance(vol, dict) else ("", vol)
        problems.append(
            f"{where}: the {kind} mount at {target} is not a named volume; the host has no checkout"
        )
        return None, False
    source = str(vol["source"])
    target = _exec_escape(str(vol["target"]))
    ro = ["ro"] if vol.get("read_only") else []
    if source in bind_volumes:
        # One container: a private SELinux label. Shared: the shared one.
        label = "Z" if len(users_of_volume.get(source, ())) == 1 else "z"
        return f"Volume={settings.data_mount}/{source}:{target}:{','.join(ro + [label])}", True
    if source in top_volumes:
        return f"Volume={source}.volume:{target}:{','.join(ro + ['z'])}", False
    problems.append(f"{where}: volume `{source}` is not declared at the top level")
    return None, False


def _depends_on(svc: dict) -> dict[str, dict]:
    deps = svc.get("depends_on") or {}
    if isinstance(deps, list):
        return {d: {"condition": "service_started"} for d in deps}
    return {d: (how or {}) for d, how in deps.items()}


def _words(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _tmpfs(svc: dict) -> list[str]:
    t = svc.get("tmpfs") or []
    return [t] if isinstance(t, str) else list(t)


def _healthcheck(svc: dict) -> dict | None:
    hc = svc.get("healthcheck")
    if not hc or _health_disabled(hc) or not hc.get("test"):
        return None
    return hc


def _health_disabled(hc: dict) -> bool:
    return bool(hc.get("disable")) or hc.get("test") == ["NONE"]


def _health_lines(svc: dict, notify: bool, where: str, problems: list[str]) -> list[str]:
    if _health_disabled(svc.get("healthcheck") or {}):
        return ["HealthCmd=none"]
    hc = _healthcheck(svc)
    if hc is None:
        return []
    test = hc["test"]
    if isinstance(test, str):
        cmd = test
    elif test[0] == "CMD-SHELL":
        cmd = test[1]
    elif test[0] == "CMD":
        cmd = json.dumps(test[1:])
    else:
        problems.append(f"{where}: healthcheck test {test!r} is not CMD or CMD-SHELL")
        return []
    lines = [f"HealthCmd={_exec_escape(cmd)}"]
    for key, unit_key in (
        ("interval", "HealthInterval"),
        ("timeout", "HealthTimeout"),
        ("start_period", "HealthStartPeriod"),
    ):
        if hc.get(key):
            lines.append(f"{unit_key}={hc[key]}")
    if hc.get("retries"):
        lines.append(f"HealthRetries={hc['retries']}")
    if notify:
        # Started only once healthy: what `condition: service_healthy` waits for.
        lines.append("Notify=healthy")
    return lines


def _limits(svc: dict, where: str, problems: list[str]) -> list[str]:
    deploy = {k: v for k, v in (svc.get("deploy") or {}).items() if v not in (None, {}, [])}
    problems.extend(
        f"{where}: deploy.{key} is not supported"
        for key in sorted(set(deploy) - {"resources", "replicas"})
    )
    if deploy.get("replicas", 1) not in (0, 1):
        problems.append(f"{where}: deploy.replicas above 1 is not supported")
    resources = {k: v for k, v in (deploy.get("resources") or {}).items() if v}
    limits = resources.get("limits") or {}
    problems.extend(
        f"{where}: deploy.resources.{key} is not supported"
        for key in sorted(set(resources) - {"limits"})
    )
    problems.extend(
        f"{where}: deploy.resources.limits.{key} is not supported"
        for key in sorted(k for k, v in limits.items() if v and k not in ("memory", "cpus"))
    )
    args = []
    if memory := limits.get("memory") or svc.get("mem_limit"):
        args.append(f"--memory={memory}")
    if limits.get("cpus"):
        args.append(f"--cpus={limits['cpus']}")
    return [f"PodmanArgs={' '.join(args)}"] if args else []


_DURATION = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001, "us": 0.000001, "µs": 0.000001}


def _seconds(value) -> int | None:
    if isinstance(value, int | float):
        return math.ceil(value)
    parts = re.findall(r"(\d+(?:\.\d+)?)(h|ms|us|µs|m|s)", str(value))
    if not parts or "".join(n + u for n, u in parts) != str(value):
        return None
    return math.ceil(sum(float(n) * _DURATION[u] for n, u in parts))


def _unescape_dollars(value):
    """compose prints every literal `$` as `$$`."""
    if isinstance(value, str):
        return value.replace("$$", "$")
    if isinstance(value, list):
        return [_unescape_dollars(v) for v in value]
    if isinstance(value, dict):
        return {k: _unescape_dollars(v) for k, v in value.items()}
    return value


def _exec_escape(text: str) -> str:
    """Quadlet copies these onto ExecStart=, where systemd expands `%` and `$`."""
    return text.replace("%", "%%").replace("$", "$$")


def _exec_words(words: list) -> str:
    out = []
    for w in map(str, words):
        w = _exec_escape(w)
        if w and not re.search(r'[\s"\'\\]', w):
            out.append(w)
        else:
            out.append('"' + w.replace("\\", "\\\\").replace('"', '\\"') + '"')
    return " ".join(out)
