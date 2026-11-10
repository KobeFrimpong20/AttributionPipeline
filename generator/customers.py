"""Simulates one customer (one generator worker) across all of their visits."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List

from events import Event
from journeys import Customer, new_customer, simulate_visit

MAX_VISITS = 20
RETURN_AFTER_ORDER = 0.70
# Without an order, the return chance starts here and grows with each visit.
RETURN_WITHOUT_ORDER_START = 0.50
RETURN_WITHOUT_ORDER_STEP = 0.05
RETURN_WITHOUT_ORDER_CAP = 0.70
# Longer than the 30-minute session timeout, so each visit is its own session.
VISIT_GAP_SECONDS = (31 * 60.0, 3 * 60 * 60.0)


@dataclass(frozen=True)
class CustomerResult:
    events: List[Event]
    customer: Customer  # final state
    visit_orders: List[bool]  # one entry per visit: did it end in an order


def return_probability(ordered: bool, visits_so_far: int) -> float:
    """Returns the chance the customer comes back after their latest visit."""
    if ordered:
        return RETURN_AFTER_ORDER
    grown = RETURN_WITHOUT_ORDER_START + RETURN_WITHOUT_ORDER_STEP * (visits_so_far - 1)
    return min(grown, RETURN_WITHOUT_ORDER_CAP)


def simulate_customer(seed: int, start: datetime) -> CustomerResult:
    """Runs every visit for one customer. The same seed always gives the same result."""
    rng = random.Random(seed)
    customer = new_customer(rng)
    events: List[Event] = []
    visit_orders: List[bool] = []
    visit_start = start

    while True:
        result = simulate_visit(rng, customer, visit_start)
        customer = result.customer
        events.extend(result.events)
        visit_orders.append(result.ordered)
        if customer.visits >= MAX_VISITS:
            break
        if rng.random() >= return_probability(result.ordered, customer.visits):
            break
        visit_start = result.ended_at + timedelta(seconds=rng.uniform(*VISIT_GAP_SECONDS))

    return CustomerResult(events=events, customer=customer, visit_orders=visit_orders)
