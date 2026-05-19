"""Render bundled DigitalOcean cloud-config templates."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

DEFAULT_TIMEZONE = "Asia/Dili"


def bootstrap_template_path() -> Path:
    """Path to the shipped ``droplet-bootstrap.yaml`` template."""
    return Path(files("catalpa_tooling").joinpath("cloud_config/droplet-bootstrap.yaml"))


def render_droplet_bootstrap(*, timezone: str = DEFAULT_TIMEZONE) -> str:
    """Return cloud-config user-data for a standard Docker deploy droplet."""
    tz = timezone.strip()
    if not tz:
        raise ValueError("timezone must be non-empty")
    template = bootstrap_template_path().read_text(encoding="utf-8")
    if "{timezone}" not in template:
        raise RuntimeError("droplet-bootstrap.yaml must contain {timezone} placeholder")
    rendered = template.replace("{timezone}", tz)
    first_line = rendered.splitlines()[0] if rendered else ""
    if first_line.strip() != "#cloud-config":
        raise RuntimeError("droplet-bootstrap.yaml must start with #cloud-config")
    return rendered
