"""Simulates customer journeys as lists of events.

Everything here is a pure function of its arguments: randomness comes from the
`rng` passed in and time from `start`, with no I/O. The same seed therefore always
produces the same journey. generate.py handles pacing and talking to ClickHouse.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Literal, Optional, Sequence, Tuple

# Channel mix: a weight for each channel, drawn independently for every session.
CHANNELS: Tuple[str, ...] = ("google_ads", "facebook", "email", "organic", "direct", "affiliate")
CHANNEL_WEIGHTS: Tuple[int, ...] = (30, 20, 15, 20, 10, 5)
# Channels you can run a campaign on. organic and direct have no utm_campaign.
CAMPAIGN_CHANNELS = frozenset({"google_ads", "facebook", "email", "affiliate"})
CAMPAIGNS: Tuple[str, ...] = ("brand", "retargeting", "seasonal_sale")

# Funnel rates, from the spec.
CART_RATE = 0.25  # share of sessions that add to cart
ORDER_RATE = 0.35  # share of carts that become orders
PAGE_VIEW_RATE = 0.30  # share of sessions with an extra page_view as browsing noise

# Edge cases. The first three apply only to customers who place an order, because
# they need a server event to create the identity links they exercise.
REPEAT_CUSTOMER_SHARE = 0.20  # ordering customers who place 2+ orders
MULTI_DEVICE_SHARE = 0.10  # ordering customers who use a second device
NO_TOUCH_SHARE = 0.01  # all customers: an order with no client events at all
LATE_EVENT_SHARE = 0.02  # events delivered well after they happened
DUPLICATE_EVENT_SHARE = 0.01  # events delivered twice with the same event_id

# Real gaps between visits last days. They are compressed to minutes so that full
# journeys show up in the tables while you watch.
SESSION_GAP_SECONDS = (30.0, 300.0)
STEP_GAP_SECONDS = (1.0, 20.0)  # between events in one session
LATE_DELAY_SECONDS = (30.0, 300.0)
DUPLICATE_DELAY_SECONDS = (1.0, 30.0)

# How far a session gets through the funnel.
Stage = Literal["view", "cart", "order"]


@dataclass(frozen=True)
class Event:
    """One row for attribution.events. received_at is left to its server DEFAULT."""

    event_id: uuid.UUID
    event_time: datetime
    anonymous_id: str
    user_id: str
    session_id: str
    event_type: str
    source: str
    channel: str
    utm_campaign: str
    product_id: str
    order_id: str
    revenue: Decimal


@dataclass(frozen=True)
class Delivery:
    """An event plus when the pipeline receives it, which may be after event_time."""

    deliver_at: float
    event: Event


@dataclass(frozen=True)
class SessionPlan:
    """What one visit will do, decided before any events are built."""

    anonymous_id: str
    stage: Stage


@dataclass(frozen=True)
class Journey:
    """All deliveries for one customer."""

    deliveries: List[Delivery]
    # A device a later customer may share. Set only for ordering customers using
    # their own device, so that each shared device ends up with exactly two users.
    shareable_anonymous_id: Optional[str]


def new_uuid(rng: random.Random) -> uuid.UUID:
    """Returns a UUID drawn from rng, so that seeded runs are reproducible."""
    return uuid.UUID(int=rng.getrandbits(128), version=4)


def new_id(rng: random.Random, prefix: str) -> str:
    """Returns a readable ID such as 'anon_3f2a...'."""
    return f"{prefix}_{new_uuid(rng).hex[:16]}"


def draw_stage(rng: random.Random) -> Stage:
    """Runs one session through the funnel: view -> cart (25%) -> order (35% of carts)."""
    if rng.random() >= CART_RATE:
        return "view"
    return "order" if rng.random() < ORDER_RATE else "cart"


def with_order_count(rng: random.Random, stages: Sequence[Stage], repeat: bool) -> List[Stage]:
    """Adjusts stages that contain an order so the customer is repeat or not.

    The natural funnel rarely gives anyone two orders, so the repeat share is set
    explicitly: a repeat customer gets at least 2 orders and everyone else exactly 1.
    """
    result = list(stages)
    order_indexes = [i for i, stage in enumerate(result) if stage == "order"]
    if repeat and len(order_indexes) < 2:
        others = [i for i, stage in enumerate(result) if stage != "order"]
        if others:
            result[rng.choice(others)] = "order"
        else:
            result.append("order")  # A single session, so add a return visit.
    elif not repeat:
        for i in order_indexes[1:]:
            result[i] = "cart"
    return result


def with_two_server_sessions(stages: Sequence[Stage]) -> List[Stage]:
    """Ensures 2+ sessions reach the server, so that both devices get an identity link."""
    result = list(stages)
    if sum(stage != "view" for stage in result) >= 2:
        return result
    if "view" in result:
        result[result.index("view")] = "cart"
        return result
    return ["cart", *result]  # A single ordering session, so add an earlier visit.


def plan_customer(
    rng: random.Random,
    shared_anonymous_id: Optional[str],
) -> Tuple[List[SessionPlan], Optional[str]]:
    """Decides a customer's sessions and devices.

    Returns the session plans and the device, if any, a later customer may share.
    """
    stages: List[Stage] = [draw_stage(rng) for _ in range(rng.randint(1, 5))]
    has_order = "order" in stages
    primary_id = new_id(rng, "anon")

    if not has_order:
        return [SessionPlan(primary_id, stage) for stage in stages], None

    stages = with_order_count(rng, stages, rng.random() < REPEAT_CUSTOMER_SHARE)

    if shared_anonymous_id is not None:
        # Shared device: this customer browses on a device an earlier customer used.
        # The newer identity link replaces the older one in identity_links.
        return [SessionPlan(shared_anonymous_id, stage) for stage in stages], None

    if rng.random() < MULTI_DEVICE_SHARE:
        # Multi-device: the last server session is on a second device. Both devices
        # then carry this user_id on a server event, so both get identity links.
        stages = with_two_server_sessions(stages)
        last_server = max(i for i, stage in enumerate(stages) if stage != "view")
        second_id = new_id(rng, "anon")
        return (
            [
                SessionPlan(second_id if i >= last_server else primary_id, stage)
                for i, stage in enumerate(stages)
            ],
            primary_id,
        )

    return [SessionPlan(primary_id, stage) for stage in stages], primary_id


def draw_campaign(rng: random.Random, channel: str) -> str:
    """Returns a utm_campaign for campaign channels, or '' for the others."""
    return rng.choice(CAMPAIGNS) if channel in CAMPAIGN_CHANNELS else ""


def draw_revenue(rng: random.Random) -> Decimal:
    """Returns an order value between 10.00 and 300.00, as an exact Decimal."""
    return Decimal(rng.randint(1_000, 30_000)) / 100


def make_event(
    rng: random.Random,
    event_seconds: float,
    anonymous_id: str,
    user_id: str,
    session_id: str,
    event_type: str,
    source: str,
    channel: str = "",
    utm_campaign: str = "",
    product_id: str = "",
    order_id: str = "",
    revenue: Decimal = Decimal("0.00"),
) -> Event:
    """Builds an Event, converting epoch seconds to a timezone-aware UTC datetime."""
    return Event(
        event_id=new_uuid(rng),
        event_time=datetime.fromtimestamp(event_seconds, tz=timezone.utc),
        anonymous_id=anonymous_id,
        user_id=user_id,
        session_id=session_id,
        event_type=event_type,
        source=source,
        channel=channel,
        utm_campaign=utm_campaign,
        product_id=product_id,
        order_id=order_id,
        revenue=revenue,
    )


def build_session(
    rng: random.Random,
    plan: SessionPlan,
    user_id: str,
    start: float,
) -> Tuple[List[Event], float]:
    """Builds one session's events and returns them with the time of the last event.

    Channel rules, which later attribution queries rely on:
    - The first client event, always a product_view, carries the channel. It is the
      touchpoint.
    - Other client events (page_view noise) have an empty channel, so they do not
      count as extra touches.
    - Server events repeat the session's channel so that funnel_minute can group them
      by channel. Touch queries ignore them by filtering on source = 'client'.
    """
    session_id = new_id(rng, "sess")
    channel = rng.choices(CHANNELS, weights=CHANNEL_WEIGHTS)[0]
    campaign = draw_campaign(rng, channel)
    product_id = f"prod_{rng.randint(1, 500):03d}"
    now = start

    def advance() -> float:
        nonlocal now
        now += rng.uniform(*STEP_GAP_SECONDS)
        return now

    events = [
        make_event(
            rng, now, plan.anonymous_id, "", session_id, "product_view", "client",
            channel=channel, utm_campaign=campaign, product_id=product_id,
        )
    ]
    if rng.random() < PAGE_VIEW_RATE:
        events.append(
            make_event(rng, advance(), plan.anonymous_id, "", session_id, "page_view", "client")
        )
    if plan.stage in ("cart", "order"):
        events.append(
            make_event(
                rng, advance(), plan.anonymous_id, user_id, session_id, "add_to_cart", "server",
                channel=channel, utm_campaign=campaign, product_id=product_id,
            )
        )
    if plan.stage == "order":
        events.append(
            make_event(
                rng, advance(), plan.anonymous_id, user_id, session_id, "order_placed", "server",
                channel=channel, utm_campaign=campaign, product_id=product_id,
                order_id=new_id(rng, "ord"), revenue=draw_revenue(rng),
            )
        )
    return events, now


def build_no_touch_events(rng: random.Random, start: float) -> List[Event]:
    """Builds an order with no client events, as when an ad blocker stops tracking.

    The checkout still passes an anonymous_id, but it has no touches and channel is
    empty. Attribution queries should report this revenue as 'unattributed'.
    """
    anonymous_id = new_id(rng, "anon")
    user_id = new_id(rng, "user")
    session_id = new_id(rng, "sess")
    product_id = f"prod_{rng.randint(1, 500):03d}"
    cart_time = start + rng.uniform(*STEP_GAP_SECONDS)
    return [
        make_event(
            rng, start, anonymous_id, user_id, session_id, "add_to_cart", "server",
            product_id=product_id,
        ),
        make_event(
            rng, cart_time, anonymous_id, user_id, session_id, "order_placed", "server",
            product_id=product_id, order_id=new_id(rng, "ord"), revenue=draw_revenue(rng),
        ),
    ]


def to_deliveries(rng: random.Random, event: Event) -> List[Delivery]:
    """Applies delivery noise: an event may arrive late, twice, or both.

    A duplicate has the same event_id and event_time. MergeTree keeps both rows.
    """
    deliver_at = event.event_time.timestamp()
    if rng.random() < LATE_EVENT_SHARE:
        deliver_at += rng.uniform(*LATE_DELAY_SECONDS)
    deliveries = [Delivery(deliver_at, event)]
    if rng.random() < DUPLICATE_EVENT_SHARE:
        deliveries.append(Delivery(deliver_at + rng.uniform(*DUPLICATE_DELAY_SECONDS), event))
    return deliveries


def build_journey(
    rng: random.Random,
    start: float,
    shared_anonymous_id: Optional[str] = None,
) -> Journey:
    """Builds every delivery for one customer, with the first session at `start`."""
    if rng.random() < NO_TOUCH_SHARE:
        events = build_no_touch_events(rng, start)
        shareable_id = None
    else:
        plans, shareable_id = plan_customer(rng, shared_anonymous_id)
        user_id = new_id(rng, "user")
        events = []
        session_start = start
        for plan in plans:
            session_events, session_end = build_session(rng, plan, user_id, session_start)
            events.extend(session_events)
            session_start = session_end + rng.uniform(*SESSION_GAP_SECONDS)

    deliveries = [delivery for event in events for delivery in to_deliveries(rng, event)]
    return Journey(deliveries, shareable_id)
