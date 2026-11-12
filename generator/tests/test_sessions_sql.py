"""Integration tests for the sessions refresh (clickhouse/init/02_sessions.sql).

They run the real refreshable view in a scratch database on the ClickHouse from
docker-compose, and are skipped when it isn't reachable. Credentials come from the
CH_USER / CH_PASSWORD environment variables, or the repo's .env.
"""

import os
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INIT = REPO / "clickhouse" / "init"
DB = "sessions_test"
COLUMNS = ["event_id", "event_type", "event_time", "received_at", "cookie_id", "user_id",
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


def statements(path, wanted):
    """Returns the statements in `path` that mention `wanted`, retargeted to the scratch DB."""
    sql = re.sub(r"--[^\n]*", "", path.read_text())
    out = []
    for stmt in (s.strip() for s in sql.split(";")):
        if stmt and wanted in stmt:
            stmt = stmt.replace("attribution.", f"{DB}.")
            # Tests trigger every run themselves.
            out.append(stmt.replace("REFRESH EVERY 5 MINUTE", "REFRESH EVERY 1 YEAR"))
    return out


class SessionsRefreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = connect()
        # Recent times, so the refresh's two-partition filter keeps them.
        cls.t0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=2)

    def setUp(self):
        c = self.client
        c.command(f"DROP DATABASE IF EXISTS {DB}")
        c.command(f"CREATE DATABASE {DB}")
        for stmt in statements(INIT / "01_schema.sql", "attribution.events"):
            c.command(stmt)
        for stmt in statements(INIT / "02_sessions.sql", "attribution."):
            c.command(stmt)
        self.received = datetime.now(timezone.utc)

    @classmethod
    def tearDownClass(cls):
        cls.client.command(f"DROP DATABASE IF EXISTS {DB}")

    # ---- helpers ---------------------------------------------------------------

    def at(self, minutes):
        return self.t0 + timedelta(minutes=minutes)

    def event(self, cookie, minutes, event_id=None, landing=False, channel="", user="",
              order="", revenue="0.00", source="client"):
        return {"event_id": event_id or uuid.uuid4(), "cookie": cookie, "time": self.at(minutes),
                "landing": landing, "channel": channel, "user": user, "order": order,
                "revenue": Decimal(revenue), "source": source}

    def insert(self, *events):
        """Inserts events as one arrival; received_at moves forward on every call."""
        self.received += timedelta(seconds=10)
        rows = [(e["event_id"], "order_placed" if e["order"] else "product_viewed", e["time"],
                 self.received, e["cookie"], e["user"], e["source"], e["landing"], e["channel"],
                 e["order"], e["revenue"], {}) for e in events]
        self.client.insert(f"{DB}.events", rows, column_names=COLUMNS)

    def refresh(self):
        self.client.command(f"SYSTEM REFRESH VIEW {DB}.mv_sessions")
        self.client.command(f"SYSTEM WAIT VIEW {DB}.mv_sessions")

    def sessions(self, cookie):
        rows = self.client.query(f"""
            SELECT session_start, min(first_event), max(last_event),
                   arraySort(groupUniqArrayMerge(event_ids)),
                   groupUniqArrayIfMerge(landings), groupUniqArrayIfMerge(orders),
                   argMaxIfMerge(user_id)
            FROM {DB}.sessions WHERE cookie_id = %(c)s
            GROUP BY session_start ORDER BY session_start""", parameters={"c": cookie}).result_rows
        utc = lambda t: t.replace(tzinfo=timezone.utc)
        return [{"key": utc(k), "first": utc(f), "last": utc(l), "ids": list(ids),
                 "landings": lands, "orders": ords, "user": u} for k, f, l, ids, lands, ords, u in rows]

    def fingerprint(self):
        return self.client.query(f"""
            SELECT count(), sum(cityHash64(cookie_id, session_start, f, l, ids, o)) FROM (
              SELECT cookie_id, session_start, min(first_event) f, max(last_event) l,
                     arraySort(groupUniqArrayMerge(event_ids)) ids, arraySort(groupUniqArrayIfMerge(orders)) o
              FROM {DB}.sessions GROUP BY cookie_id, session_start)""").result_rows[0]

    # ---- cases -------------------------------------------------------------------

    def test_duplicates_in_one_batch_and_across_runs(self):
        dup = uuid.uuid4()
        self.insert(self.event("c1", 0, landing=True, channel="email"),
                    self.event("c1", 1, event_id=dup), self.event("c1", 1, event_id=dup))
        self.refresh()
        self.insert(self.event("c1", 1, event_id=dup))  # the copy arrives in a later run
        self.refresh()
        [s] = self.sessions("c1")
        self.assertEqual(len(s["ids"]), 2)
        self.assertEqual(s["ids"].count(dup), 1)

    def test_session_continued_in_a_later_run_keeps_its_key(self):
        self.insert(self.event("c2", 0, landing=True, channel="organic"), self.event("c2", 5))
        self.refresh()
        self.insert(self.event("c2", 20))
        self.refresh()
        [s] = self.sessions("c2")
        self.assertEqual(s["key"], self.at(0))
        self.assertEqual(s["last"], self.at(20))
        self.assertEqual(len(s["ids"]), 3)

    def test_late_event_before_the_start_moves_first_event_not_the_key(self):
        self.insert(self.event("c3", 0), self.event("c3", 5))
        self.refresh()
        self.insert(self.event("c3", -8, landing=True, channel="facebook"))  # late landing
        self.refresh()
        [s] = self.sessions("c3")
        self.assertEqual(s["key"], self.at(0))
        self.assertEqual(s["first"], self.at(-8))
        self.assertEqual(s["landings"], [(self.at(-8).replace(tzinfo=None), "facebook")])

    def test_gap_over_30_minutes_starts_a_new_session(self):
        self.insert(self.event("c4", 0), self.event("c4", 31))  # same batch
        self.refresh()
        self.insert(self.event("c4", 62))  # later run, 31 min after the last
        self.refresh()
        self.assertEqual([s["key"] for s in self.sessions("c4")], [self.at(0), self.at(31), self.at(62)])

    def test_bridging_event_keeps_two_sessions_and_joins_the_earlier(self):
        self.insert(self.event("c5", 0), self.event("c5", 5))
        self.insert(self.event("c5", 40), self.event("c5", 45))
        self.refresh()
        self.insert(self.event("c5", 22))  # within 30 min of both stored sessions
        self.refresh()
        first, second = self.sessions("c5")
        self.assertEqual((first["key"], first["last"]), (self.at(0), self.at(22)))
        self.assertEqual((second["key"], second["first"]), (self.at(40), self.at(40)))

    def test_orders_count_once_and_user_id_survives_anonymous_events(self):
        self.insert(self.event("c6", 0, landing=True, channel="google_ads"),
                    self.event("c6", 2, user="user_1", order="o1", revenue="12.50"),
                    self.event("c6", 2, user="user_1", order="o1", revenue="12.50", source="server"),
                    self.event("c6", 3))  # anonymous event after the order
        self.refresh()
        self.insert(self.event("c6", 2, user="user_1", order="o1", revenue="12.50", source="server"))
        self.refresh()
        [s] = self.sessions("c6")
        self.assertEqual([(o[0], o[1]) for o in s["orders"]], [("o1", Decimal("12.50"))])
        self.assertEqual(s["user"], "user_1")

    def test_rerun_without_new_data_changes_nothing(self):
        self.insert(self.event("c7", 0, landing=True, channel="direct"), self.event("c7", 3, order="o7", revenue="5.00"))
        self.refresh()
        before = self.fingerprint()
        self.refresh()  # re-reads the same rows through the 1-minute overlap
        self.refresh()
        self.assertEqual(self.fingerprint(), before)

    def test_watermark_advances_and_old_rows_are_not_reread(self):
        self.insert(self.event("c8", 0))
        self.refresh()
        first_wm = self.client.query(f"SELECT max(max_received_at) FROM {DB}.sessions").result_rows[0][0]
        self.received += timedelta(minutes=10)
        self.insert(self.event("c8", 4))
        self.refresh()
        second_wm = self.client.query(f"SELECT max(max_received_at) FROM {DB}.sessions").result_rows[0][0]
        self.assertGreater(second_wm, first_wm)
        # Only the new event was read: the second run appended exactly one partial row.
        rows = self.client.query(f"SELECT count() FROM {DB}.sessions").result_rows[0][0]
        self.assertEqual(rows, 2)


if __name__ == "__main__":
    unittest.main()
