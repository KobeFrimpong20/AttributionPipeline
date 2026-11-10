import random
import unittest

from state_machine import (
    CART_ONLY,
    EVENT_TYPES,
    REPEAT_CUSTOMER_TRANSITIONS,
    START_WEIGHTS,
    TRANSITIONS,
    State,
    available_edges,
    boost,
    exit_probability,
    next_state,
)

S = State

EXPECTED_EXITS = {
    S.VIEW_COLLECTION: 0.25,
    S.VIEW_PRODUCT: 0.35,
    S.LOAD_PAGE: 0.30,
    S.ADD_TO_CART: 0.15,
    S.REMOVE_FROM_CART: 0.30,
    S.VIEW_CART: 0.25,
    S.START_CHECKOUT: 0.20,
    S.ADD_PAYMENT_INFO: 0.25,
    S.PLACE_ORDER: 1.00,
}


class TransitionTableTest(unittest.TestCase):
    def test_every_state_has_edges_and_an_event_type(self):
        self.assertEqual(set(TRANSITIONS), set(State))
        self.assertEqual(set(EVENT_TYPES), set(State))

    def test_start_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(p for _, p in START_WEIGHTS), 1.0)

    def test_rows_sum_to_at_most_one(self):
        for state, edges in TRANSITIONS.items():
            self.assertLessEqual(sum(p for _, p in edges), 1.0 + 1e-9, state)

    def test_exit_probabilities_match_the_design(self):
        for state, expected in EXPECTED_EXITS.items():
            self.assertAlmostEqual(exit_probability(TRANSITIONS[state]), expected, msg=state)


class BoostTest(unittest.TestCase):
    def test_boost_lowers_each_exit_by_five_points(self):
        for state, expected in EXPECTED_EXITS.items():
            boosted_exit = exit_probability(REPEAT_CUSTOMER_TRANSITIONS[state])
            if state is S.PLACE_ORDER:
                self.assertAlmostEqual(boosted_exit, 1.0)
            else:
                self.assertAlmostEqual(boosted_exit, expected - 0.05, msg=state)

    def test_boost_keeps_edge_ratios(self):
        for state, edges in TRANSITIONS.items():
            base = dict(edges)
            boosted = dict(REPEAT_CUSTOMER_TRANSITIONS[state])
            self.assertEqual(set(base), set(boosted))
            # Every edge in a row is scaled by the same factor.
            scales = {round(boosted[nxt] / base[nxt], 9) for nxt in base}
            self.assertLessEqual(len(scales), 1, state)

    def test_boost_never_takes_more_than_the_exit(self):
        table = {S.ADD_PAYMENT_INFO: ((S.PLACE_ORDER, 0.98),)}
        boosted = boost(table, 0.05)
        self.assertAlmostEqual(exit_probability(boosted[S.ADD_PAYMENT_INFO]), 0.0)


class EmptyCartTest(unittest.TestCase):
    def test_empty_cart_disables_cart_only_edges(self):
        for state in State:
            targets = {nxt for nxt, _ in available_edges(state, True, TRANSITIONS)}
            self.assertFalse(targets & CART_ONLY, state)

    def test_view_cart_with_empty_cart_exits_085(self):
        edges = available_edges(S.VIEW_CART, True, TRANSITIONS)
        self.assertEqual([nxt for nxt, _ in edges], [S.ADD_TO_CART])
        self.assertAlmostEqual(exit_probability(edges), 0.85)

    def test_full_cart_keeps_all_edges(self):
        self.assertEqual(available_edges(S.VIEW_CART, False, TRANSITIONS), TRANSITIONS[S.VIEW_CART])

    def test_next_state_follows_probabilities(self):
        rng = random.Random(7)
        n = 100_000
        outcomes = [next_state(rng, S.ADD_PAYMENT_INFO, False, TRANSITIONS) for _ in range(n)]
        self.assertAlmostEqual(outcomes.count(S.PLACE_ORDER) / n, 0.75, delta=0.01)
        self.assertAlmostEqual(outcomes.count(None) / n, 0.25, delta=0.01)

    def test_place_order_always_exits(self):
        rng = random.Random(1)
        self.assertTrue(all(next_state(rng, S.PLACE_ORDER, False, TRANSITIONS) is None for _ in range(100)))


if __name__ == "__main__":
    unittest.main()
