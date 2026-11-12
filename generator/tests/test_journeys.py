import random
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from journeys import Customer, new_customer, simulate_visit
from state_machine import EVENT_TYPES, State

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
TYPE_TO_STATE = {v: k for k, v in EVENT_TYPES.items()}
LANDING_TYPES = {EVENT_TYPES[State.VIEW_COLLECTION], EVENT_TYPES[State.VIEW_PRODUCT]}


def many_visits(n=5_000, has_ordered=False):
    for seed in range(n):
        rng = random.Random(seed)
        customer = new_customer(rng)
        if has_ordered:
            customer = Customer(cookie_id=customer.cookie_id, has_ordered=True)
        yield simulate_visit(rng, customer, START)


class VisitShapeTest(unittest.TestCase):
    def test_first_event_is_the_only_landing(self):
        for v in many_visits():
            first = v.events[0]
            self.assertTrue(first.is_landing)
            self.assertIn(first.event_type, LANDING_TYPES)
            self.assertNotEqual(first.channel, "")
            for e in v.events[1:]:
                self.assertFalse(e.is_landing)
                self.assertEqual(e.channel, "")

    def test_visit_without_landing(self):
        landing_keys = {"utm_source", "utm_campaign", "gclid", "fbclid"}
        for seed in range(2_000):
            rng = random.Random(seed)
            v = simulate_visit(rng, new_customer(rng), START, landed=False)
            self.assertIn(v.events[0].event_type, LANDING_TYPES)
            for e in v.events:
                self.assertFalse(e.is_landing)
                self.assertEqual(e.channel, "")
                self.assertFalse(landing_keys & e.properties.keys())

    def test_cart_rules_hold(self):
        for v in many_visits():
            cart = 0
            for e in v.events:
                if e.source != "client":
                    continue
                state = TYPE_TO_STATE[e.event_type]
                if state in (State.REMOVE_FROM_CART, State.START_CHECKOUT):
                    self.assertGreater(cart, 0, e.event_type)
                if state is State.ADD_TO_CART:
                    cart += 1
                elif state is State.REMOVE_FROM_CART:
                    cart -= 1

    def test_event_times_never_go_backwards(self):
        for v in many_visits(1_000):
            times = [e.event_time for e in v.events]
            self.assertEqual(times, sorted(times))
            self.assertEqual(v.ended_at, times[-1])

    def test_event_ids_are_unique(self):
        ids = [e.event_id for v in many_visits(1_000) for e in v.events]
        self.assertEqual(len(ids), len(set(ids)))


class OrderTest(unittest.TestCase):
    def orders(self):
        return [v for v in many_visits() if v.ordered]

    def test_some_visits_order(self):
        self.assertGreater(len(self.orders()), 100)

    def test_order_emits_client_and_server_pair_and_ends_visit(self):
        for v in self.orders():
            client, server = v.events[-2], v.events[-1]
            self.assertEqual((client.event_type, server.event_type), ("order_placed", "order_placed"))
            self.assertEqual((client.source, server.source), ("client", "server"))
            self.assertEqual(client.order_id, server.order_id)
            self.assertEqual(client.revenue, server.revenue)
            self.assertEqual(client.cookie_id, server.cookie_id)
            self.assertNotEqual(client.event_id, server.event_id)
            self.assertEqual(sum(e.event_type == "order_placed" for e in v.events), 2)

    def test_revenue_equals_cart_total(self):
        for v in self.orders():
            cart = []
            for e in v.events:
                if e.event_type == "product_added_to_cart":
                    cart.append((e.properties["product_id"], Decimal(e.properties["price"])))
                elif e.event_type == "product_removed_from_cart":
                    idx = next(i for i, (pid, _) in enumerate(cart) if pid == e.properties["product_id"])
                    cart.pop(idx)
            self.assertEqual(v.events[-1].revenue, sum((p for _, p in cart), Decimal("0.00")))
            self.assertGreater(v.events[-1].revenue, 0)

    def test_user_id_set_from_checkout_onwards(self):
        for v in self.orders():
            seen_checkout = False
            for e in v.events:
                seen_checkout = seen_checkout or e.event_type == "checkout_started"
                self.assertEqual(bool(e.user_id), seen_checkout, e.event_type)
            self.assertTrue(v.customer.has_ordered)

    def test_non_order_visits_have_no_order_fields(self):
        for v in many_visits(1_000):
            if not v.ordered:
                self.assertTrue(all(e.order_id == "" and e.source == "client" for e in v.events))


class OrderRateTest(unittest.TestCase):
    def test_new_customer_order_rate(self):
        rate = sum(v.ordered for v in many_visits(100_000)) / 100_000
        self.assertAlmostEqual(rate, 0.064, delta=0.005)

    def test_repeat_customer_order_rate(self):
        rate = sum(v.ordered for v in many_visits(100_000, has_ordered=True)) / 100_000
        self.assertAlmostEqual(rate, 0.099, delta=0.006)


if __name__ == "__main__":
    unittest.main()
