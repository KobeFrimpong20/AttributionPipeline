"""Shared helpers for the SQL integration tests.

The tests run the real schema and refreshable views in a scratch database on the
ClickHouse from docker-compose, and are skipped when it isn't reachable. Credentials
come from the CH_USER / CH_PASSWORD environment variables, or the repo's .env.
"""

import os
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INIT = REPO / "clickhouse" / "init"
EVENT_COLUMNS = ["event_id", "event_type", "event_time", "received_at", "cookie_id", "user_id",
                 "source", "is_landing", "channel", "order_id", "revenue", "properties"]


def credentials():
    env = {}
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return (os.environ.get("CH_USER", env.get("CH_USER", "default")),
            os.environ.get("CH_PASSWORD", env.get("CH_PASS", "")))


def connect():
    """Returns a client, or skips the test module when ClickHouse isn't reachable."""
    try:
        import clickhouse_connect
        user, password = credentials()
        client = clickhouse_connect.get_client(
            host=os.environ.get("CH_HOST", "localhost"), port=int(os.environ.get("CH_PORT", "8123")),
            username=user, password=password, connect_timeout=2)
        client.command("SELECT 1")
        return client
    except Exception as e:  # driver missing or server down
        raise unittest.SkipTest(f"ClickHouse not reachable: {e}")


def statements(path, db, wanted="attribution."):
    """Returns the statements in `path` that mention `wanted`, retargeted to `db`.

    Refreshes are set to once a year so the tests trigger every run themselves.
    """
    sql = re.sub(r"--[^\n]*", "", path.read_text())
    out = []
    for stmt in (s.strip() for s in sql.split(";")):
        if stmt and wanted in stmt:
            stmt = stmt.replace("attribution.", f"{db}.")
            out.append(stmt.replace("REFRESH EVERY 5 MINUTE", "REFRESH EVERY 1 YEAR"))
    return out


def create_schema(client, db, files):
    """Creates `db` from scratch with the events table plus every statement in `files`."""
    client.command(f"DROP DATABASE IF EXISTS {db}")
    client.command(f"CREATE DATABASE {db}")
    for stmt in statements(INIT / "01_schema.sql", db, "attribution.events"):
        client.command(stmt)
    for name in files:
        for stmt in statements(INIT / name, db):
            client.command(stmt)


def refresh(client, db, *views):
    """Runs each view once, in order, waiting for it to finish."""
    for view in views:
        client.command(f"SYSTEM REFRESH VIEW {db}.{view}")
        client.command(f"SYSTEM WAIT VIEW {db}.{view}")
