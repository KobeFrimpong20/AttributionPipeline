"""Integration tests for conversions (clickhouse/init/04_conversions.sql).

They run the real chain of refreshable views (sessions, identity, profiles,
conversions) in a scratch database (see clickhouse_sql.py) and are skipped when
ClickHouse isn't reachable. Visits in a test are at least 31 minutes apart, so each
is its own session, like the generator's.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from clickhouse_sql import EVENT_COLUMNS, connect, create_schema, refresh

DB = "conversions_test"
CHAIN = ("mv_sessions", "mv_identity", "mv_profiles", "mv_conversions")


class ConversionsRefreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = connect()
        cls.t0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=6)

    @classmethod
    def tearDownClass(cls):
        cls.client.command(f"DROP DATABASE IF EXISTS {DB}")

    def setUp(self):
        create_schema(self.client, DB, ["02_sessions.sql", "03_profiles.sql", "04_conversions.sql"])
        self.received = datetime.now(timezone.utc)

    # ---- helpers ---------------------------------------------------------------

    def at(self, minutes):
        return self.t0 + timedelta(minutes=minutes)

    def landing(self, cookie, minutes, channel, user=""):
        return (uuid.uuid4(), "collection_viewed", self.at(minutes), cookie, user, "client",
                True, channel, "", Decimal("0"), {})

    def order(self, cookie, minutes, user, order_id, revenue="10.00", source="client"):
        return (uuid.uuid4(), "order_placed", self.at(minutes), cookie, user, source,
                False, "", order_id, Decimal(revenue), {"email": f"{user}@example.com"})

    def arrive(self, *events):
        """Inserts events as one arrival, 5 minutes after the last, and runs the chain."""
        self.received += timedelta(minutes=5)
        rows = [(e[0], e[1], e[2], self.received) + e[3:] for e in events]
        self.client.insert(f"{DB}.events", rows, column_names=EVENT_COLUMNS)
        refresh(self.client, DB, *CHAIN)

    def later_cycle(self):
        """A cycle of unrelated traffic, so earlier arrivals fall out of the 1-minute overlap."""
        self.arrive(self.landing(f"other_{uuid.uuid4().hex[:8]}", 0, "direct"))

    def conversion(self, order_id):
        rows = self.client.query(f"""
            SELECT conversion_type, attribution_status, first_touch, last_touch, u_shaped, touches
            FROM {DB}.conversions_current WHERE order_id = %(o)s""", parameters={"o": order_id}).result_rows
        self.assertEqual(len(rows), 1, f"expected one current stamp for {order_id}")
        ctype, status, first, last, u_shaped, touches = rows[0]
        return dict(type=ctype, status=status, first=first, last=last, touches=touches,
                    # Named tuples come back as dicts: {"channel": ..., "credit": ...}.
                    u={t["channel"]: round(t["credit"], 9) for t in u_shaped})

    def fingerprint(self):
        return self.client.query(f"""
            SELECT count(), sum(cityHash64(order_id, attribution_status, first_touch, last_touch,
                                           arraySort(u_shaped), touches))
            FROM {DB}.conversions_current""").result_rows[0]

    # ---- cases -------------------------------------------------------------------

    def test_one_two_and_four_touches(self):
        self.arrive(self.landing("A", 0, "email"), self.order("A", 1, "u1", "o1"))
        self.arrive(self.landing("B", 0, "email"), self.landing("B", 40, "facebook"),
                    self.order("B", 41, "u2", "o2"))
        self.arrive(self.landing("C", 0, "google_ads"), self.landing("C", 40, "email"),
                    self.landing("C", 80, "facebook"), self.landing("C", 120, "affiliate"),
                    self.order("C", 121, "u3", "o3"))
        self.assertEqual(self.conversion("o1")["u"], {"email": 1.0})
        self.assertEqual(self.conversion("o2")["u"], {"email": 0.5, "facebook": 0.5})
        c = self.conversion("o3")
        self.assertEqual(c["u"], {"google_ads": 0.4, "email": 0.1, "facebook": 0.1, "affiliate": 0.4})
        self.assertEqual((c["type"], c["status"], c["first"], c["last"], c["touches"]),
                         ("first_order", "attributed", "google_ads", "affiliate", 4))

    def test_same_channel_credits_are_summed(self):
        self.arrive(self.landing("D", 0, "email"), self.landing("D", 40, "facebook"),
                    self.landing("D", 80, "email"), self.order("D", 81, "u4", "o4"))
        self.assertEqual(self.conversion("o4")["u"], {"email": 0.8, "facebook": 0.2})

    def test_repeat_order_uses_lifetime_first_plus_its_window(self):
        self.arrive(self.landing("E", 0, "google_ads"), self.order("E", 1, "u5", "o5a"))
        self.arrive(self.landing("E", 40, "email"), self.landing("E", 80, "facebook"),
                    self.order("E", 81, "u5", "o5b"))
        c = self.conversion("o5b")
        self.assertEqual((c["type"], c["status"], c["first"], c["last"]),
                         ("repeat_order", "attributed", "google_ads", "facebook"))
        self.assertEqual(c["u"], {"google_ads": 0.4, "email": 0.2, "facebook": 0.4})

    def test_repeat_order_without_new_landing(self):
        self.arrive(self.landing("F", 0, "organic"), self.order("F", 1, "u6", "o6a"))
        self.arrive(self.order("F", 40, "u6", "o6b"))  # came back with no landing
        c = self.conversion("o6b")
        self.assertEqual((c["status"], c["first"], c["last"], c["u"]),
                         ("returning_no_new_touch", "organic", "", {"organic": 1.0}))

    def test_first_order_without_any_landing_is_unattributed(self):
        self.arrive(self.order("G", 0, "u7", "o7"))
        c = self.conversion("o7")
        self.assertEqual((c["status"], c["first"], c["last"], c["u"], c["touches"]),
                         ("unattributed", "", "", {}, 0))

    def test_late_landing_restamps_the_order(self):
        self.arrive(self.landing("H", 40, "email"), self.order("H", 41, "u8", "o8"))
        self.assertEqual(self.conversion("o8")["u"], {"email": 1.0})
        self.later_cycle()
        self.arrive(self.landing("H", 0, "facebook"))  # an earlier landing arrives late
        c = self.conversion("o8")
        self.assertEqual((c["first"], c["last"], c["u"]), ("facebook", "email", {"facebook": 0.5, "email": 0.5}))

    def test_linking_a_cookie_with_an_earlier_landing_restamps(self):
        self.arrive(self.landing("I", 40, "email"), self.order("I", 41, "u9", "o9"))
        self.arrive(self.landing("J", 0, "affiliate"))  # the phone, anonymous for now
        self.assertEqual(self.conversion("o9")["first"], "email")
        self.later_cycle()
        self.arrive(self.order("J", 90, "u9", "o9b"))  # checks out on the phone: J is linked
        c = self.conversion("o9")
        self.assertEqual((c["first"], c["u"]), ("affiliate", {"affiliate": 0.5, "email": 0.5}))

    def test_landing_after_the_order_does_not_count(self):
        self.arrive(self.landing("K", 0, "email"), self.order("K", 1, "u10", "o10"),
                    self.landing("K", 40, "facebook"))
        c = self.conversion("o10")
        self.assertEqual((c["first"], c["last"], c["u"]), ("email", "email", {"email": 1.0}))

    def test_duplicate_orders_and_reruns_change_nothing(self):
        self.arrive(self.landing("L", 0, "direct"), self.order("L", 1, "u11", "o11"),
                    self.order("L", 1, "u11", "o11", source="server"))
        self.arrive(self.order("L", 1, "u11", "o11", source="server"))  # a duplicate send later
        before = self.fingerprint()
        refresh(self.client, DB, *CHAIN)
        refresh(self.client, DB, *CHAIN)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.client.query(
            f"SELECT count(), sum(revenue) FROM {DB}.conversions_current").result_rows[0],
            (1, Decimal("10.00")))


if __name__ == "__main__":
    unittest.main()
