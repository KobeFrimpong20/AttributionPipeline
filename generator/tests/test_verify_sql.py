"""End-to-end test of ground truth and verification (05_verification.sql, verify.sql).

The real scheduler runs for three simulated hours in 5-minute steps. Each step's
deliveries, customers and truth rows are inserted into a scratch database, and the four
refreshable views run. Verification must then pass; after corrupting one order,
dropping one and inventing one, it must catch all three. Skipped when ClickHouse isn't
reachable.
"""

import itertools
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from clickhouse_sql import INIT, REPO, connect
from generate import (CUSTOMER_COLUMNS, EVENT_COLUMNS, EXPECTED_COLUMNS, customer_row,
                      expected_row, to_row)
from scheduler import new_scheduler, step
from verify import statements as verify_statements

DB, TDB = "vrf_pipeline", "vrf_truth"  # names that contain neither "test." nor "attribution."
CHAIN = ("mv_sessions", "mv_identity", "mv_profiles", "mv_conversions")


def retarget(sql):
    sql = sql.replace("test.", f"{TDB}.").replace("attribution.", f"{DB}.")
    sql = sql.replace("IF NOT EXISTS test", f"IF NOT EXISTS {TDB}").replace("REFRESH EVERY 5 MINUTE", "REFRESH EVERY 1 YEAR")
    return sql


def schema_statements():
    for name in ("01_schema.sql", "02_sessions.sql", "03_profiles.sql", "04_conversions.sql", "05_verification.sql"):
        sql = re.sub(r"--[^\n]*", "", (INIT / name).read_text())
        yield from (retarget(s.strip()) for s in sql.split(";") if s.strip())


class VerifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = c = connect()
        for db in (DB, TDB):
            c.command(f"DROP DATABASE IF EXISTS {db}")
        c.command(f"CREATE DATABASE {DB}")
        for stmt in schema_statements():
            c.command(stmt)
        cls.summary_sql, cls.mismatch_sql = (retarget(s) for s in verify_statements(
            REPO / "clickhouse" / "queries" / "verify.sql"))

        now = datetime.now(timezone.utc).replace(microsecond=0)
        sched = new_scheduler(itertools.count(30_000_000), now - timedelta(hours=3))
        t = now - timedelta(hours=3)
        cls.expected_rows = 0
        while t <= now:
            result = step(sched, t, arrivals_per_sec=0.15)
            if result.deliveries:
                c.insert(f"{DB}.events", [to_row(d) + (d.deliver_at,) for d in result.deliveries],
                         column_names=list(EVENT_COLUMNS) + ["received_at"])
            if result.customers:
                c.insert(f"{TDB}.customers", [customer_row(x) for x in result.customers],
                         column_names=list(CUSTOMER_COLUMNS))
            if result.expected:
                c.insert(f"{TDB}.expected_attribution", [expected_row(e) for e in result.expected],
                         column_names=list(EXPECTED_COLUMNS))
                cls.expected_rows += len(result.expected)
            for view in CHAIN:
                c.command(f"SYSTEM REFRESH VIEW {DB}.{view}")
                c.command(f"SYSTEM WAIT VIEW {DB}.{view}")
            t += timedelta(minutes=5)

    @classmethod
    def tearDownClass(cls):
        for db in (DB, TDB):
            cls.client.command(f"DROP DATABASE IF EXISTS {db}")

    def summary(self):
        r = self.client.query(self.summary_sql)
        return dict(zip(r.column_names, r.result_rows[0]))

    def test_1_pipeline_passes_and_then_catches_each_failure(self):
        s = self.summary()
        self.assertGreater(s["judged"], 20, s)
        self.assertEqual((s["matched"], s["wrong"], s["missing"], s["invented"], s["pass"]),
                         (s["judged"], 0, 0, 0, True), s)

        c = self.client
        judged = [r[0] for r in c.query(
            f"SELECT order_id FROM {TDB}.verification WHERE outcome = 'matched' ORDER BY order_id LIMIT 2").result_rows]
        wrong, missing = judged
        # A later stamp with the wrong first touch replaces the right one.
        c.command(f"""
            INSERT INTO {DB}.conversions
            SELECT * REPLACE ('not_a_channel' AS first_touch, now64(3) + INTERVAL 1 HOUR AS stamped_at)
            FROM {DB}.conversions FINAL WHERE order_id = '{wrong}'""")
        c.command(f"DELETE FROM {DB}.conversions WHERE order_id = '{missing}'")
        c.insert(f"{DB}.conversions",
                 [(f"order_{uuid.uuid4().hex[:16]}", "user_x", datetime.now(timezone.utc) - timedelta(hours=1),
                   Decimal("1.00"), "first_order", "attributed", "email", "email", [("email", 1.0)], 1,
                   datetime(1970, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc))],
                 column_names=["order_id", "profile_key", "order_time", "revenue", "conversion_type",
                               "attribution_status", "first_touch", "last_touch", "u_shaped", "touches",
                               "source_received_at", "stamped_at"])

        s = self.summary()
        self.assertEqual((s["wrong"], s["missing"], s["invented"], s["pass"]), (1, 1, 1, False), s)
        outcomes = sorted(r[0] for r in c.query(self.mismatch_sql).result_rows)
        self.assertEqual(outcomes, ["invented", "missing", "wrong"])


if __name__ == "__main__":
    unittest.main()
