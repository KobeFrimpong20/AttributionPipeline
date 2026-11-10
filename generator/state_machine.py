"""The journey state machine: states, transition probabilities and the step rule.

One run of the machine is one visit. Every state can end the visit: the exit
probability is whatever is left after a state's outgoing edges. Everything here is
pure; randomness comes from the `rng` passed in.
"""

from __future__ import annotations

import random
from enum import Enum
from typing import Dict, FrozenSet, Mapping, Optional, Tuple


class State(Enum):
    VIEW_COLLECTION = "view_collection"
    VIEW_PRODUCT = "view_product"
    LOAD_PAGE = "load_page"
    ADD_TO_CART = "add_to_cart"
    REMOVE_FROM_CART = "remove_from_cart"
    VIEW_CART = "view_cart"
    START_CHECKOUT = "start_checkout"
    ADD_PAYMENT_INFO = "add_payment_info"
    PLACE_ORDER = "place_order"


# Outgoing edges of one state, as (next state, probability) pairs.
Edges = Tuple[Tuple[State, float], ...]
TransitionTable = Mapping[State, Edges]

S = State

# Where a visit starts. Both start states are landing pages.
START_WEIGHTS: Edges = (
    (S.VIEW_COLLECTION, 0.50),
    (S.VIEW_PRODUCT, 0.50),
)
LANDING_STATES: FrozenSet[State] = frozenset({S.VIEW_COLLECTION, S.VIEW_PRODUCT})

# Exit probability of each state = 1 - sum of its edges.
TRANSITIONS: Dict[State, Edges] = {
    S.VIEW_COLLECTION: ((S.VIEW_PRODUCT, 0.55), (S.LOAD_PAGE, 0.20)),
    S.VIEW_PRODUCT: (
        (S.VIEW_COLLECTION, 0.15),
        (S.VIEW_PRODUCT, 0.30),  # a different product
        (S.LOAD_PAGE, 0.05),
        (S.ADD_TO_CART, 0.15),
    ),
    S.LOAD_PAGE: ((S.VIEW_COLLECTION, 0.30), (S.VIEW_PRODUCT, 0.40)),
    S.ADD_TO_CART: (
        (S.VIEW_PRODUCT, 0.10),
        (S.REMOVE_FROM_CART, 0.05),
        (S.VIEW_CART, 0.45),
        (S.START_CHECKOUT, 0.25),
    ),
    S.REMOVE_FROM_CART: ((S.VIEW_CART, 0.50), (S.ADD_TO_CART, 0.20)),
    S.VIEW_CART: (
        (S.REMOVE_FROM_CART, 0.10),
        (S.ADD_TO_CART, 0.15),
        (S.START_CHECKOUT, 0.50),
    ),
    S.START_CHECKOUT: ((S.VIEW_CART, 0.15), (S.ADD_PAYMENT_INFO, 0.65)),
    S.ADD_PAYMENT_INFO: ((S.PLACE_ORDER, 0.75),),
    S.PLACE_ORDER: (),  # every visit ends after an order
}

# Edges that need at least one item in the cart. With an empty cart they are
# disabled and their probability goes to exit.
CART_ONLY: FrozenSet[State] = frozenset({S.REMOVE_FROM_CART, S.START_CHECKOUT})

EVENT_TYPES: Dict[State, str] = {
    S.VIEW_COLLECTION: "collection_viewed",
    S.VIEW_PRODUCT: "product_viewed",
    S.LOAD_PAGE: "page_loaded",
    S.ADD_TO_CART: "product_added_to_cart",
    S.REMOVE_FROM_CART: "product_removed_from_cart",
    S.VIEW_CART: "cart_viewed",
    S.START_CHECKOUT: "checkout_started",
    S.ADD_PAYMENT_INFO: "payment_info_added",
    S.PLACE_ORDER: "order_placed",
}

# How much less likely a repeat customer is to leave at each step.
REPEAT_CUSTOMER_BOOST = 0.05


def exit_probability(edges: Edges) -> float:
    """Returns the chance of ending the visit given a state's outgoing edges."""
    return 1.0 - sum(p for _, p in edges)


def boost(table: TransitionTable, amount: float = REPEAT_CUSTOMER_BOOST) -> Dict[State, Edges]:
    """Returns a table where each state's exit is `amount` lower.

    The removed exit probability is added to the outgoing edges in proportion to
    their weights. States without edges (place order) are unchanged.
    """
    boosted: Dict[State, Edges] = {}
    for state, edges in table.items():
        total = sum(p for _, p in edges)
        if total == 0:
            boosted[state] = edges
            continue
        shift = min(amount, exit_probability(edges))
        boosted[state] = tuple((nxt, p + shift * p / total) for nxt, p in edges)
    return boosted


REPEAT_CUSTOMER_TRANSITIONS: Dict[State, Edges] = boost(TRANSITIONS)


def available_edges(state: State, cart_empty: bool, table: TransitionTable) -> Edges:
    """Returns the edges that can be taken from `state`, applying the empty-cart rule."""
    edges = table[state]
    if not cart_empty:
        return edges
    return tuple((nxt, p) for nxt, p in edges if nxt not in CART_ONLY)


def start_state(rng: random.Random) -> State:
    """Draws the first state of a visit."""
    states = [s for s, _ in START_WEIGHTS]
    weights = [p for _, p in START_WEIGHTS]
    return rng.choices(states, weights=weights)[0]


def next_state(
    rng: random.Random, state: State, cart_empty: bool, table: TransitionTable
) -> Optional[State]:
    """Draws the next state, or returns None when the visit ends."""
    roll = rng.random()
    cumulative = 0.0
    for nxt, p in available_edges(state, cart_empty, table):
        cumulative += p
        if roll < cumulative:
            return nxt
    return None
