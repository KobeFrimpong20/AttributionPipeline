import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from attribution import expected_orders, first_refresh, restamp_deadline, stamp
from customers import CustomerResult, VisitSummary, simulate_customer
from delivery import Delivery
from events import Event
from journeys import Customer

DAY = datetime(2026, 3, 10, tzinfo=timezone.utc)


def at(minutes):
    return DAY + timedelta(minutes=minutes)


def credits(s):
    return {k: round(v, 9) for k, v in s.u_shaped.items()}


class StampTest(unittest.TestCase):
    """The same rule cases as the conversions SQL tests, on the Python side."""

    def test_one_two_and_four_touches(self):
        self.assertEqual(credits(stamp([(at(0), "email")], [at(1)], 0)), {"email": 1.0})
        self.assertEqual(credits(stamp([(at(0), "email"), (at(40), "facebook")], [at(41)], 0)),
                         {"email": 0.5, "facebook": 0.5})
        s = stamp([(at(0), "google_ads"), (at(40), "email"), (at(80), "facebook"), (at(120), "affiliate")],
                  [at(121)], 0)
        self.assertEqual(credits(s), {"google_ads": 0.4, "email": 0.1, "facebook": 0.1, "affiliate": 0.4})
        self.assertEqual((s.status, s.conversion_type, s.first_touch, s.last_touch, s.touches),
                         ("attributed", "first_order", "google_ads", "affiliate", 4))

    def test_same_channel_summed(self):
        s = stamp([(at(0), "email"), (at(40), "facebook"), (at(80), "email")], [at(81)], 0)
        self.assertEqual(credits(s), {"email": 0.8, "facebook": 0.2})

    def test_repeat_order_uses_lifetime_first_plus_window(self):
        landings = [(at(0), "google_ads"), (at(40), "email"), (at(80), "facebook")]
        s = stamp(landings, [at(1), at(81)], 1)
        self.assertEqual((s.conversion_type, s.first_touch, s.last_touch), ("repeat_order", "google_ads", "facebook"))
        self.assertEqual(credits(s), {"google_ads": 0.4, "email": 0.2, "facebook": 0.4})

    def test_no_new_touch_and_unattributed(self):
        s = stamp([(at(0), "organic")], [at(1), at(40)], 1)
        self.assertEqual((s.status, s.last_touch, credits(s)), ("returning_no_new_touch", "", {"organic": 1.0}))
        s = stamp([], [at(0)], 0)
        self.assertEqual((s.status, s.first_touch, credits(s), s.touches), ("unattributed", "", {}, 0))

    def test_landing_after_the_order_does_not_count(self):
        s = stamp([(at(0), "email"), (at(40), "facebook")], [at(1)], 0)
        self.assertEqual((s.last_touch, credits(s)), ("email", {"email": 1.0}))


class WindowTest(unittest.TestCase):
    def test_first_refresh_rounds_up_to_five_minutes(self):
        self.assertEqual(first_refresh(at(10)), at(10))
        self.assertEqual(first_refresh(at(10) + timedelta(milliseconds=1)), at(15))

    def test_deadline_is_end_of_the_next_day(self):
        self.assertEqual(restamp_deadline(DAY + timedelta(hours=23, minutes=59)), DAY + timedelta(days=2))


def customer(events, delivered_at=None):
    """A hand-built CustomerResult. delivered_at maps event index -> delivery time."""
    delivered_at = delivered_at or {}
    deliveries = [Delivery(event=e, deliver_at=delivered_at.get(i, e.event_time), held=i in delivered_at)
                  for i, e in enumerate(events)]
    cookies = list(dict.fromkeys(e.cookie_id for e in events))
    return CustomerResult(
        events=events, customer=Customer(cookie_id=cookies[-1], user_id="user_1"),
        visits=[VisitSummary(first_event=0, ordered=True, landed=True, cookie_id=cookies[0])],
        cookies=cookies, late_sender=bool(delivered_at), deliveries=deliveries)


def event(cookie, minutes, channel="", user="", order_id=""):
    return Event(event_id=uuid.uuid4(), event_type="order_placed" if order_id else "collection_viewed",
                 event_time=at(minutes), cookie_id=cookie, user_id=user, source="client",
                 is_landing=bool(channel), channel=channel, order_id=order_id,
                 revenue=Decimal("10.00") if order_id else Decimal("0.00"))


class ExpectedOrdersTest(unittest.TestCase):
    def test_landing_on_a_device_that_never_logs_in_is_a_device_gap(self):
        r = customer([event("phone", 0, "facebook"), event("laptop", 40, "email"),
                      event("laptop", 41, user="user_1", order_id="o1")])
        e = expected_orders(r, seed=1)["o1"]
        self.assertEqual((e.real.first_touch, e.reachable.first_touch), ("facebook", "email"))
        self.assertEqual((e.gap_device, e.gap_window), (True, False))

    def test_link_after_the_window_is_a_window_gap(self):
        # The phone's landing is first, but the phone is only logged in 3 days later,
        # after the order has left the re-stamp window.
        r = customer([event("phone", 0, "facebook"), event("laptop", 40, "email"),
                      event("laptop", 41, user="user_1", order_id="o1"),
                      event("phone", 3 * 24 * 60, user="user_1", order_id="o2")])
        e = expected_orders(r, seed=1)["o1"]
        self.assertEqual((e.real.first_touch, e.reachable.first_touch), ("facebook", "email"))
        self.assertEqual((e.gap_device, e.gap_window), (False, True))

    def test_late_landing_counts_only_if_refreshed_before_the_deadline(self):
        events = [event("laptop", 40, "email"), event("laptop", 41, user="user_1", order_id="o1"),
                  event("laptop", 0, "facebook")]
        deadline = restamp_deadline(at(41))
        just_in = expected_orders(customer(events, {2: deadline - timedelta(minutes=5)}), 1)["o1"]
        too_late = expected_orders(customer(events, {2: deadline - timedelta(minutes=5) + timedelta(seconds=1)}), 1)["o1"]
        self.assertEqual((just_in.reachable.first_touch, just_in.gap_window), ("facebook", False))
        self.assertEqual((too_late.reachable.first_touch, too_late.gap_window), ("email", True))

    def test_single_cookie_customers_have_no_gap(self):
        start = datetime(2026, 1, 1, 6, tzinfo=timezone.utc)
        for seed in range(3_000):
            r = simulate_customer(seed, start)
            if len(r.cookies) > 1:
                continue
            for e in expected_orders(r, seed).values():
                self.assertTrue(e.real.same_as(e.reachable))


if __name__ == "__main__":
    unittest.main()
