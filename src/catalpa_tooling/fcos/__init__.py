"""Fedora CoreOS host files rendered from a project's compose file (issue #182, "option B").

``dk <env> fcos render`` turns the environment's resolved compose file into rootless Podman
quadlets for the ``fcos.user`` account, plus their settings files and the edge's Caddy JSON, and
writes them under ``docker/envs/<env>/generated/fcos/``. The render is committed, and
``dk <env> fcos check`` compares a fresh render with it byte for byte, so what reaches a host is
what was reviewed.

The compose file stays the description of the stack, runnable with plain ``docker compose up``:
nothing here needs a change to it, or to the project's Caddyfile, that only works under ``dk``.
"""
