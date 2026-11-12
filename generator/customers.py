"""Simulates one customer (one generator worker) across all of their visits."""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import List, Sequence, TypeVar

from delivery import (
    Delivery,
    add_duplicates,
    deliver_visit,
    delivery_rng,
    duplicates_rng,
    is_late_sender,
)
from events import Event, new_id
from journeys import Customer, new_customer, simulate_visit

MAX_VISITS = 20
RETURN_AFTER_ORDER = 0.70
# Without an order, the return chance starts here and grows with each visit.
RETURN_WITHOUT_ORDER_START = 0.50
RETURN_WITHOUT_ORDER_STEP = 0.05
RETURN_WITHOUT_ORDER_CAP = 0.70
# Chance that a visit has no landing (bookmark or open tab), drawn per visit.
NO_LANDING_SHARE = 0.03
# New cookies, drawn from their own RNG stream. The first visit always uses the primary
# cookie. A new cookie is logged out until the person checks out on it.
SECOND_DEVICE_SHARE = 0.20  # customers who also visit from a second device
SECOND_DEVICE_VISIT_SHARE = 0.30  # their return visits that use it
CLEARED_COOKIE_SHARE = 0.03  # return visits whose device gets a fresh cookie from now on
# Longer than the 30-minute session timeout, so each visit is its own session.
VISIT_GAP_SECONDS = (31 * 60.0, 3 * 60 * 60.0)


T = TypeVar("T")


@dataclass(frozen=True)
class VisitSummary:
    first_event: int  # index of the visit's first event in `events`
    ordered: bool
    landed: bool  # False when the visit had no landing
    cookie_id: str  # the cookie the visit was made on


@dataclass(frozen=True)
class CustomerResult:
    events: List[Event]
    customer: Customer  # final state
    visits: List[VisitSummary]
    cookies: List[str]  # every cookie used, in order of first use
    late_sender: bool
    # Every event at least once, in event order; a duplicate follows its original.
    deliveries: List[Delivery]


def landing_rng(seed: int) -> random.Random:
    """Returns the RNG that decides which visits have no landing."""
    return random.Random(f"{seed}:no_landing")


def devices_rng(seed: int) -> random.Random:
    """Returns the RNG for second devices and cleared cookies."""
    return random.Random(f"{seed}:devices")


def split_by_visit(items: Sequence[T], visits: Sequence[VisitSummary]) -> List[List[T]]:
    """Splits per-event items (events, or deliveries without duplicates) into visits."""
    bounds = [v.first_event for v in visits] + [len(items)]
    return [list(items[a:b]) for a, b in zip(bounds, bounds[1:])]


def return_probability(ordered: bool, visits_so_far: int) -> float:
    """Returns the chance the customer comes back after their latest visit."""
    if ordered:
        return RETURN_AFTER_ORDER
    grown = RETURN_WITHOUT_ORDER_START + RETURN_WITHOUT_ORDER_STEP * (visits_so_far - 1)
    return min(grown, RETURN_WITHOUT_ORDER_CAP)


def simulate_customer(seed: int, start: datetime) -> CustomerResult:
    """Runs every visit for one customer. The same seed always gives the same result."""
    rng = random.Random(seed)
    send_rng = delivery_rng(seed)
    late_sender = is_late_sender(send_rng)
    dup_rng = duplicates_rng(seed)
    land_rng = landing_rng(seed)
    dev_rng = devices_rng(seed)
    customer = new_customer(rng)
    has_second_device = dev_rng.random() < SECOND_DEVICE_SHARE
    device_cookies = [customer.cookie_id, new_id(dev_rng, "cookie")]  # primary, second
    cookies: List[str] = []
    events: List[Event] = []
    deliveries: List[Delivery] = []
    visits: List[VisitSummary] = []
    visit_start = start

    while True:
        landed = land_rng.random() >= NO_LANDING_SHARE
        device = 0
        if customer.visits > 0:  # return visit: pick a device, maybe with a cleared cookie
            use_second = dev_rng.random() < SECOND_DEVICE_VISIT_SHARE
            device = 1 if has_second_device and use_second else 0
            if dev_rng.random() < CLEARED_COOKIE_SHARE:
                device_cookies[device] = new_id(dev_rng, "cookie")
        cookie = device_cookies[device]
        if cookie not in cookies:
            cookies.append(cookie)
        result = simulate_visit(rng, replace(customer, cookie_id=cookie), visit_start, landed=landed)
        customer = result.customer
        visits.append(VisitSummary(first_event=len(events), ordered=result.ordered, landed=landed, cookie_id=cookie))
        events.extend(result.events)
        deliveries.extend(add_duplicates(dup_rng, deliver_visit(send_rng, result.events, late_sender)))
        if customer.visits >= MAX_VISITS:
            break
        if rng.random() >= return_probability(result.ordered, customer.visits):
            break
        visit_start = result.ended_at + timedelta(seconds=rng.uniform(*VISIT_GAP_SECONDS))

    return CustomerResult(
        events=events,
        customer=customer,
        visits=visits,
        cookies=cookies,
        late_sender=late_sender,
        deliveries=deliveries,
    )
