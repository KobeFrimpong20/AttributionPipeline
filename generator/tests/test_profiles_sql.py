"""Integration tests for identity and profiles (clickhouse/init/03_profiles.sql).

They run the real chain of refreshable views (sessions, identity, profiles) in a
scratch database (see clickhouse_sql.py) and are skipped when ClickHouse isn't
reachable.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from clickhouse_sql import EVENT_COLUMNS, connect, create_schema, refresh

DB = "profiles_test"
CHAIN = ("mv_sessions", "mv_identity", "mv_profiles")


class ProfilesRefreshTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = connect()
        cls.t0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)

    @classmethod
    def tearDownClass(cls):
        cls.client.command(f"DROP DATABASE IF EXISTS {DB}")

    def setUp(self):
        create_schema(self.client, DB, ["02_sessions.sql", "03_profiles.sql"])
        self.received = datetime.now(timezone.utc)

    # ---- helpers ---------------------------------------------------------------

    def at(self, minutes):
        return self.t0 + timedelta(minutes=minutes)

    def naive(self, minutes):
        return self.at(minutes).replace(tzinfo=None)

    def event(self, cookie, minutes, channel="", user="", email="", name="", event_id=None):
        props = {k: v for k, v in (("email", email), ("name", name)) if v}
        return (event_id or uuid.uuid4(), "checkout_started" if user else "product_viewed",
                self.at(minutes), cookie, user, "client", bool(channel), channel, "", Decimal("0"), props)

    def arrive(self, *events):
        """Inserts events as one arrival and runs the whole refresh chain.

        Arrivals are 5 minutes apart, like real refresh cycles, so the 1-minute overlap
        can't re-read an earlier arrival and hide a missing re-emit.
        """
        self.received += timedelta(minutes=5)
        rows = [(e[0], e[1], e[2], self.received) + e[3:] for e in events]
        self.client.insert(f"{DB}.events", rows, column_names=EVENT_COLUMNS)
        refresh(self.client, DB, *CHAIN)

    def later_cycle(self):
        """Another cycle with unrelated traffic, so earlier arrivals fall out of the overlap.

        The watermark is the latest received_at, so the 1-minute overlap always re-reads
        the previous arrival. Real anonymous history is usually many cycles old.
        """
        self.arrive(self.event(f"other_{uuid.uuid4().hex[:8]}", 0))

    def profiles(self):
        rows = self.client.query(f"""
            SELECT profile_key, kind, arraySort(cookies), length(sessions), first_touch_channel,
                   first_touch_time, email, name
            FROM {DB}.profiles_current ORDER BY profile_key""").result_rows
        return {r[0]: dict(kind=r[1], cookies=r[2], sessions=r[3], channel=r[4],
                           time=r[5], email=r[6], name=r[7]) for r in rows}

    def fingerprint(self):
        return self.client.query(f"""
            SELECT count(), sum(cityHash64(profile_key, kind, arraySort(cookies), arraySort(sessions),
                                           ft, arraySort(landings), email, name))
            FROM {DB}.profiles_current""").result_rows[0]

    # ---- cases -------------------------------------------------------------------

    def test_anonymous_history_moves_into_the_person_at_checkout(self):
        self.arrive(self.event("A", 0, channel="google_ads"))
        self.assertEqual(self.profiles()["A"]["kind"], "cookie")  # provisional for now
        self.later_cycle()
        self.arrive(self.event("A", 40, user="u1", email="u1@example.com", name="Ada Smith"))
        profiles = self.profiles()
        self.assertNotIn("A", profiles)  # the provisional profile is hidden once linked
        p = profiles["u1"]
        self.assertEqual((p["kind"], p["cookies"], p["sessions"]), ("user", ["A"], 2))
        self.assertEqual((p["channel"], p["time"]), ("google_ads", self.naive(0)))
        self.assertEqual((p["email"], p["name"]), ("u1@example.com", "Ada Smith"))

    def test_second_cookie_joins_the_same_person_and_first_touch_spans_cookies(self):
        self.arrive(self.event("A", 0, channel="email"), self.event("A", 2, user="u2", email="u2@example.com"))
        self.arrive(self.event("B", -50, channel="facebook"))  # phone, earlier, anonymous
        self.assertEqual(self.profiles()["u2"]["cookies"], ["A"])
        self.later_cycle()
        self.arrive(self.event("B", 60, user="u2", email="u2@example.com"))  # logs in on the phone
        p = self.profiles()["u2"]
        self.assertEqual(p["cookies"], ["A", "B"])
        self.assertEqual((p["channel"], p["time"]), ("facebook", self.naive(-50)))
        self.assertNotIn("B", self.profiles())

    def test_late_earlier_landing_updates_first_touch(self):
        self.arrive(self.event("C", 0, channel="direct"), self.event("C", 1, user="u3", email="u3@example.com"))
        self.arrive(self.event("C", -20, channel="affiliate"))  # late, from before the session
        p = self.profiles()["u3"]
        self.assertEqual((p["channel"], p["time"]), ("affiliate", self.naive(-20)))

    def test_never_identified_cookie_stays_provisional(self):
        self.arrive(self.event("D", 0, channel="organic"))
        self.arrive(self.event("D", 45, channel="email"))
        p = self.profiles()["D"]
        self.assertEqual((p["kind"], p["cookies"], p["sessions"], p["channel"]), ("cookie", ["D"], 2, "organic"))

    def test_no_landing_means_no_first_touch(self):
        self.arrive(self.event("E", 0, user="u5", email="u5@example.com"))
        p = self.profiles()["u5"]
        self.assertEqual((p["channel"], p["time"]), ("", None))

    def test_duplicates_and_reruns_change_nothing(self):
        dup = uuid.uuid4()
        self.arrive(self.event("F", 0, channel="google_ads", event_id=dup),
                    self.event("F", 1, user="u6", email="u6@example.com"))
        self.arrive(self.event("F", 0, channel="google_ads", event_id=dup))  # duplicate in a later run
        before = self.fingerprint()
        refresh(self.client, DB, *CHAIN)  # reruns re-read the 1-minute overlap
        refresh(self.client, DB, *CHAIN)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.profiles()["u6"]["sessions"], 1)


if __name__ == "__main__":
    unittest.main()
