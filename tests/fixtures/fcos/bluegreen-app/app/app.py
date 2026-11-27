"""A Partisipa-shaped test app for dk's blue/green deploy: one colour of a site.

`python app.py migrate` is the colour's migrate oneshot; `python app.py serve` is its web.
Its settings are the colour's dotenv secret, mounted at /app/.env as Partisipa's is: the
database (in the site account, on the host's loopback at 169.254.1.3) and the colour's
name. Made-up data only.
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg


def read_dotenv(path: str = "/app/.env") -> dict[str, str]:
    with open(path) as f:
        lines = [line.strip() for line in f]
    return dict(
        line.split("=", 1) for line in lines if line and not line.startswith("#")
    )


E = {**os.environ, **read_dotenv()}


def dsn() -> str:
    return E["DATABASE_URL"] + "?connect_timeout=3"


def migrate() -> None:
    with psycopg.connect(dsn()) as c:
        c.execute(
            "create table if not exists notes (id serial primary key, name text not null)"
        )
        c.execute(
            "create table if not exists deploys (id serial primary key, units text, app text, at timestamptz default now())"
        )
        c.execute(
            "insert into deploys (units, app) values (%s, %s)",
            (E["UNITS_VERSION"], E["APP_VERSION"]),
        )
    if E.get("BREAK_MIGRATION"):
        # A release whose migration commits damage and then fails: what the freeze is for.
        with psycopg.connect(dsn()) as c:
            c.execute("drop table notes")
        sys.exit("migration failed after dropping notes")


def state() -> dict:
    with psycopg.connect(dsn()) as c:
        notes = [r[0] for r in c.execute("select name from notes order by id")]
        last = c.execute(
            "select units, app from deploys order by id desc limit 1"
        ).fetchone()
    return {
        "colour": E["COLOUR"],
        "units": E["UNITS_VERSION"],
        "app": E["APP_VERSION"],
        "notes": notes,
        "last_migrated_by": {"units": last[0], "app": last[1]} if last else None,
    }


class Handler(BaseHTTPRequestHandler):
    def send(self, code: int, body: dict) -> None:
        data = (json.dumps(body) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        try:
            body = state()
        except psycopg.Error as e:  # the database is away: report it, as /healthz must
            return self.send(503, {"error": str(e), "colour": E["COLOUR"]})
        self.send(
            200,
            {"ok": True, "colour": E["COLOUR"]} if self.path == "/healthz" else body,
        )

    def do_PUT(self) -> None:
        name = self.path.strip("/")
        with psycopg.connect(dsn()) as c:
            c.execute("insert into notes (name) values (%s)", (name,))
        self.send(201, {"stored": name, "by": E["COLOUR"]})


if __name__ == "__main__":
    if sys.argv[1:] == ["migrate"]:
        migrate()
    elif sys.argv[1:] == ["serve"]:
        ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
    else:
        sys.exit("usage: app.py migrate|serve")
