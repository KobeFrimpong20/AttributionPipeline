"""Simulates one visit (a journey) as a list of events.

A visit walks the state machine from a landing page until it exits or places an
order. Everything is a pure function of its arguments: randomness comes from the
`rng` passed in and time from `start`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from catalog import Collection, Product, pick_collection, pick_other_product, pick_product_in
from events import Event, new_id, new_uuid
from state_machine import (
    EVENT_TYPES,
    REPEAT_CUSTOMER_TRANSITIONS,
    TRANSITIONS,
    State,
    next_state,
    start_state,
)

# Channel mix for landings, drawn independently for every visit.
CHANNELS: Tuple[str, ...] = ("google_ads", "facebook", "email", "organic", "direct", "affiliate")
CHANNEL_WEIGHTS: Tuple[int, ...] = (30, 20, 15, 20, 10, 5)
CAMPAIGNS: Tuple[str, ...] = ("brand", "retargeting", "seasonal_sale")
# Channels whose landings carry UTM parameters, and the click-ID key for ad networks.
UTM_CHANNELS = frozenset({"google_ads", "facebook", "email", "affiliate"})
CLICK_ID_KEYS: Dict[str, str] = {"google_ads": "gclid", "facebook": "fbclid"}

PAGE_PATHS: Tuple[str, ...] = ("/about", "/faq", "/shipping", "/returns", "/blog", "/contact")
PAYMENT_METHODS: Tuple[str, ...] = ("card", "paypal", "apple_pay")

STEP_GAP_SECONDS = (5.0, 90.0)  # between events in one visit


@dataclass(frozen=True)
class Customer:
    """What persists about a customer between visits."""

    cookie_id: str
    user_id: str = ""  # set at their first checkout
    email: str = ""
    has_ordered: bool = False
    visits: int = 0


@dataclass(frozen=True)
class VisitResult:
    events: List[Event]
    ordered: bool
    ended_at: datetime
    customer: Customer  # updated after the visit


def new_customer(rng: random.Random) -> Customer:
    """Returns an anonymous customer with a fresh browser cookie."""
    return Customer(cookie_id=new_id(rng, "cookie"))


def identify(rng: random.Random, customer: Customer) -> Customer:
    """Gives the customer a user ID and email if they don't have one yet."""
    if customer.user_id:
        return customer
    user_id = new_id(rng, "user")
    return replace(customer, user_id=user_id, email=f"{user_id}@example.com")


def draw_channel(rng: random.Random) -> str:
    return rng.choices(CHANNELS, weights=CHANNEL_WEIGHTS)[0]


def landing_properties(rng: random.Random, channel: str) -> Dict[str, str]:
    """Returns the UTM and click-ID properties a landing from `channel` carries."""
    props: Dict[str, str] = {}
    if channel in UTM_CHANNELS:
        props["utm_source"] = channel
        props["utm_campaign"] = rng.choice(CAMPAIGNS)
    if channel in CLICK_ID_KEYS:
        props[CLICK_ID_KEYS[channel]] = new_uuid(rng).hex
    return props


def cart_total(cart: List[Product]) -> Decimal:
    return sum((p.price for p in cart), Decimal("0.00"))


def simulate_visit(
    rng: random.Random, customer: Customer, start: datetime, landed: bool = True
) -> VisitResult:
    """Walks the state machine once and returns the visit's events.

    Customers who have ordered before use the repeat-customer transitions. With
    `landed=False` the visit has no landing: the customer came back through a bookmark
    or an open tab, so no event carries a channel, UTM tags or a click ID.
    """
    table = REPEAT_CUSTOMER_TRANSITIONS if customer.has_ordered else TRANSITIONS
    channel = draw_channel(rng) if landed else ""

    events: List[Event] = []
    cart: List[Product] = []
    collection: Optional[Collection] = None
    last_viewed: Optional[Product] = None
    cart_token = ""
    checkout_token = ""
    ordered = False
    now = start
    previous: Optional[State] = None
    state: Optional[State] = start_state(rng)

    while state is not None:
        props: Dict[str, str] = {}
        order_id = ""
        revenue = Decimal("0.00")

        if state is State.VIEW_COLLECTION:
            collection = pick_collection(rng)
            props["collection_id"] = collection.collection_id
        elif state is State.VIEW_PRODUCT:
            if previous is State.VIEW_COLLECTION and collection is not None:
                last_viewed = pick_product_in(rng, collection)
            elif last_viewed is not None:
                last_viewed = pick_other_product(rng, last_viewed)
            else:
                last_viewed = pick_product_in(rng, pick_collection(rng))
            props.update(
                product_id=last_viewed.product_id,
                collection_id=last_viewed.collection_id,
                price=str(last_viewed.price),
            )
        elif state is State.LOAD_PAGE:
            props["page_path"] = rng.choice(PAGE_PATHS)
        elif state is State.ADD_TO_CART:
            # Every path to add to cart passes a product view first.
            assert last_viewed is not None
            cart_token = cart_token or new_id(rng, "cart")
            cart.append(last_viewed)
            props.update(
                product_id=last_viewed.product_id,
                price=str(last_viewed.price),
                quantity="1",
                cart_token=cart_token,
            )
        elif state is State.REMOVE_FROM_CART:
            removed = cart.pop(rng.randrange(len(cart)))
            props.update(product_id=removed.product_id, cart_token=cart_token)
        elif state is State.VIEW_CART:
            props.update(
                cart_token=cart_token,
                item_count=str(len(cart)),
                cart_total=str(cart_total(cart)),
            )
        elif state is State.START_CHECKOUT:
            customer = identify(rng, customer)
            checkout_token = checkout_token or new_id(rng, "checkout")
            props.update(
                checkout_token=checkout_token,
                cart_token=cart_token,
                email=customer.email,
                item_count=str(len(cart)),
                cart_total=str(cart_total(cart)),
            )
        elif state is State.ADD_PAYMENT_INFO:
            props.update(checkout_token=checkout_token, payment_method=rng.choice(PAYMENT_METHODS))
        elif state is State.PLACE_ORDER:
            order_id = new_id(rng, "order")
            revenue = cart_total(cart)
            ordered = True
            props.update(checkout_token=checkout_token, email=customer.email, item_count=str(len(cart)))

        is_landing = landed and not events
        client_event = Event(
            event_id=new_uuid(rng),
            event_type=EVENT_TYPES[state],
            event_time=now,
            cookie_id=customer.cookie_id,
            user_id=customer.user_id,
            source="client",
            is_landing=is_landing,
            channel=channel if is_landing else "",
            order_id=order_id,
            revenue=revenue,
            properties={**(landing_properties(rng, channel) if is_landing else {}), **props},
        )
        events.append(client_event)
        if state is State.PLACE_ORDER:
            # The server records the same order separately; the cookie ID is added
            # upstream before ingestion.
            events.append(replace(client_event, event_id=new_uuid(rng), source="server", is_landing=False, channel=""))

        previous = state
        state = next_state(rng, state, cart_empty=not cart, table=table)
        if state is not None:
            now = now + timedelta(seconds=rng.uniform(*STEP_GAP_SECONDS))

    customer = replace(customer, visits=customer.visits + 1, has_ordered=customer.has_ordered or ordered)
    return VisitResult(events=events, ordered=ordered, ended_at=now, customer=customer)
