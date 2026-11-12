import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import customers

from customers import (
    MAX_VISITS,
    NO_LANDING_SHARE,
    return_probability,
    simulate_customer,
    split_by_visit,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
SESSION_TIMEOUT = timedelta(minutes=30)


class ReturnProbabilityTest(unittest.TestCase):
    def test_after_an_order(self):
        for visits in (1, 5, 19):
            self.assertAlmostEqual(return_probability(True, visits), 0.70)

    def test_without_an_order_grows_then_caps(self):
        expected = [0.50, 0.55, 0.60, 0.65, 0.70, 0.70, 0.70]
        actual = [return_probability(False, v) for v in range(1, 8)]
        for a, e in zip(actual, expected):
            self.assertAlmostEqual(a, e)


class SimulateCustomerTest(unittest.TestCase):
    def test_same_seed_same_output(self):
        self.assertEqual(simulate_customer(42, START), simulate_customer(42, START))

    def test_different_seeds_differ(self):
        self.assertNotEqual(simulate_customer(1, START).events, simulate_customer(2, START).events)

    def test_one_cookie_per_customer(self):
        for seed in range(500):
            r = simulate_customer(seed, START)
            self.assertEqual({e.cookie_id for e in r.events}, set(r.cookies))
            self.assertEqual(r.events[0].cookie_id, r.cookies[0])  # first visit: primary cookie
            for summary, events in zip(r.visits, split_by_visit(r.events, r.visits)):
                self.assertEqual({e.cookie_id for e in events}, {summary.cookie_id})

    def test_visit_cap_and_gaps(self):
        for seed in range(3_000):
            r = simulate_customer(seed, START)
            self.assertLessEqual(len(r.visits), MAX_VISITS)
            self.assertEqual(r.customer.visits, len(r.visits))
            visits = split_by_visit(r.events, r.visits)
            self.assertTrue(all(visits))
            # The gap before each later visit is longer than the session timeout.
            for prev, nxt in zip(visits, visits[1:]):
                self.assertGreater(nxt[0].event_time - prev[-1].event_time, SESSION_TIMEOUT)

    def test_boost_applies_only_after_first_order(self):
        # Order rates by whether the customer had already ordered before the visit.
        new = [0, 0]
        repeat = [0, 0]
        for seed in range(60_000):
            has_ordered = False
            for ordered in (v.ordered for v in simulate_customer(seed, START).visits):
                bucket = repeat if has_ordered else new
                bucket[0] += 1
                bucket[1] += ordered
                has_ordered = has_ordered or ordered
        self.assertAlmostEqual(new[1] / new[0], 0.064, delta=0.006)
        self.assertGreater(repeat[1] / repeat[0], 0.085)

    def test_landings_match_the_visit_summary(self):
        for seed in range(3_000):
            r = simulate_customer(seed, START)
            for summary, events in zip(r.visits, split_by_visit(r.events, r.visits)):
                landings = [e for e in events if e.is_landing]
                self.assertEqual(len(landings), int(summary.landed))
                if summary.landed:
                    self.assertIs(landings[0], events[0])
                self.assertEqual(summary.ordered, any(e.order_id for e in events))

    def test_no_landing_share(self):
        visits = no_landing = 0
        orders_without_touch = 0
        for seed in range(20_000):
            r = simulate_customer(seed, START)
            visits += len(r.visits)
            no_landing += sum(not v.landed for v in r.visits)
            orders_without_touch += sum(v.ordered and not v.landed for v in r.visits)
        self.assertAlmostEqual(no_landing / visits, NO_LANDING_SHARE, delta=0.004)
        self.assertGreater(orders_without_touch, 0)

    def test_visits_before_first_checkout_are_anonymous(self):
        # Everything before the person's first checkout is anonymous, on every cookie.
        with_history = 0
        for seed in range(3_000):
            r = simulate_customer(seed, START)
            first_order = next((i for i, v in enumerate(r.visits) if v.ordered), None)
            if not first_order:
                continue
            with_history += 1
            visits = split_by_visit(r.events, r.visits)
            checkout = next(i for i, e in enumerate(r.events) if e.event_type == "checkout_started")
            for e in r.events[:checkout]:
                self.assertEqual(e.user_id, "")
            self.assertEqual(len(visits), len(r.visits))
        self.assertGreater(with_history, 0)

    def test_user_id_persists_after_identification(self):
        for seed in range(3_000):
            r = simulate_customer(seed, START)
            # Per cookie: anonymous until the person checks out on it, then always identified.
            for cookie in r.cookies:
                user_ids = [e.user_id for e in r.events if e.cookie_id == cookie]
                first = next((i for i, u in enumerate(user_ids) if u), None)
                if first is not None:
                    self.assertEqual(set(user_ids[first:]), {r.customer.user_id})
                    self.assertEqual(set(user_ids[:first]), {""} if first else set())
                    checkout = next(e for e in r.events if e.cookie_id == cookie and e.user_id)
                    self.assertIn(checkout.event_type, ("checkout_started", "order_placed"))


class DevicesTest(unittest.TestCase):
    def return_visits(self, n=20_000):
        for seed in range(n):
            r = simulate_customer(seed, START)
            for i in range(1, len(r.visits)):
                yield r, i

    def test_cleared_cookie_rate(self):
        # With no second devices, every new cookie on a return visit is a cleared cookie.
        with mock.patch.object(customers, "SECOND_DEVICE_SHARE", 0.0):
            new = [r.visits[i].cookie_id != r.visits[i - 1].cookie_id for r, i in self.return_visits()]
        self.assertAlmostEqual(sum(new) / len(new), customers.CLEARED_COOKIE_SHARE, delta=0.006)

    def test_second_device_rates(self):
        with mock.patch.object(customers, "CLEARED_COOKIE_SHARE", 0.0):
            multi = sum(len(simulate_customer(s, START).cookies) > 1 for s in range(20_000))
            # Everyone has a second device: share of return visits made on it.
            with mock.patch.object(customers, "SECOND_DEVICE_SHARE", 1.0):
                on_second = [r.visits[i].cookie_id != r.cookies[0] for r, i in self.return_visits()]
        self.assertAlmostEqual(sum(on_second) / len(on_second), customers.SECOND_DEVICE_VISIT_SHARE, delta=0.01)
        # Only customers who return and pick the second device show two cookies.
        self.assertLess(multi / 20_000, customers.SECOND_DEVICE_SHARE)
        self.assertGreater(multi, 0)

    def test_without_devices_every_customer_has_one_cookie(self):
        with mock.patch.multiple(customers, SECOND_DEVICE_SHARE=0.0, CLEARED_COOKIE_SHARE=0.0):
            for seed in range(1_000):
                self.assertEqual(len(simulate_customer(seed, START).cookies), 1)

    def test_devices_never_change_the_journey(self):
        for seed in range(500):
            with_devices = simulate_customer(seed, START)
            with mock.patch.multiple(customers, SECOND_DEVICE_SHARE=0.0, CLEARED_COOKIE_SHARE=0.0):
                without = simulate_customer(seed, START)
            # Same events apart from the cookie and the login state that depends on it.
            strip = lambda r: [(e.event_id, e.event_type, e.event_time, e.order_id) for e in r.events]
            self.assertEqual(strip(with_devices), strip(without))

    def test_new_cookie_is_anonymous_until_checkout_on_it(self):
        found = 0
        for seed in range(5_000):
            r = simulate_customer(seed, START)
            if not r.customer.user_id:
                continue
            identified_at = next(i for i, e in enumerate(r.events) if e.user_id)
            for cookie in r.cookies[1:]:
                on_cookie = [e for e in r.events[identified_at:] if e.cookie_id == cookie]
                if on_cookie and not on_cookie[0].user_id:
                    found += 1  # already identified elsewhere, still anonymous here
        self.assertGreater(found, 0)

    def test_name_and_email_on_checkout_and_order(self):
        for seed in range(2_000):
            r = simulate_customer(seed, START)
            for e in r.events:
                if e.event_type in ("checkout_started", "order_placed"):
                    self.assertEqual(e.properties["email"], r.customer.email)
                    self.assertEqual(e.properties["name"], r.customer.name)
                    self.assertTrue(r.customer.name)


if __name__ == "__main__":
    unittest.main()
