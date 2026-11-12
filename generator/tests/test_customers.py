import unittest
from datetime import datetime, timedelta, timezone

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
            self.assertEqual({e.cookie_id for e in r.events}, {r.customer.cookie_id})

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

    def test_user_id_persists_after_identification(self):
        for seed in range(3_000):
            r = simulate_customer(seed, START)
            user_ids = [e.user_id for e in r.events]
            first = next((i for i, u in enumerate(user_ids) if u), None)
            if first is not None:
                self.assertEqual(set(user_ids[first:]), {r.customer.user_id})
                self.assertEqual(set(user_ids[:first]), {""} if first else set())


if __name__ == "__main__":
    unittest.main()
