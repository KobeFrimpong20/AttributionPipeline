"""The correct attribution for every order: the generator's ground truth.

This is written independently of the SQL in clickhouse/init/04_conversions.sql, so a
mistake in one is not copied into the other. Everything here is pure.

Each order gets three answers:
  real       Every landing the person made, on any cookie.
  visible    Only landings on cookies the person logged in on (the pipeline links a
             cookie to a person only through a checkout on it).
  reachable  Visible, and only inputs that arrived while the order could still be
             re-stamped. The pipeline re-stamps orders from the last two days, so an
             input counts only if the refresh that picks it up runs before the end of
             the day after the order's day. This is what the pipeline should produce.

The gap between them has two causes: real != visible is a landing on a device that
never logged in; visible != reachable is an input that arrived after the window.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Sequence, Tuple

from customers import CustomerResult

Landing = Tuple[datetime, str]  # (event_time, channel)

REFRESH_INTERVAL = timedelta(minutes=5)  # refreshes run on the clock: :00, :05, ...
RESTAMP_DAYS = 2  # orders are re-stamped while order day >= today - 1 day


@dataclass(frozen=True)
class Stamp:
    status: str  # attributed | unattributed | returning_no_new_touch
    conversion_type: str  # first_order | repeat_order
    first_touch: str
    last_touch: str
    u_shaped: Dict[str, float]  # channel -> credit, summed per channel
    touches: int

    def same_as(self, other: "Stamp") -> bool:
        """Equal, with credits compared to 9 decimal places."""
        rounded = lambda s: {k: round(v, 9) for k, v in s.u_shaped.items()}
        return (self.status, self.conversion_type, self.first_touch, self.last_touch, self.touches,
                rounded(self)) == (other.status, other.conversion_type, other.first_touch,
                                   other.last_touch, other.touches, rounded(other))


@dataclass(frozen=True)
class Expected:
    order_id: str
    user_id: str
    seed: int
    order_time: datetime
    revenue: Decimal
    reachable: Stamp  # what verification checks the pipeline against
    real: Stamp
    gap_device: bool  # a landing on a never-logged-in device changes the answer
    gap_window: bool  # an input that arrived after the re-stamp window changes it


def stamp(landings: Sequence[Landing], order_times: Sequence[datetime], i: int) -> Stamp:
    """Stamps order i. Landings and order times must be sorted by time.

    First touch: the first landing up to the order. Last touch: the latest landing
    after the previous order, up to this one. U-shaped: [lifetime first] + those
    window landings; 40% first, 40% last, 20% across the middle (100% for one touch,
    50/50 for two), summed per channel.
    """
    t = order_times[i]
    prev = order_times[i - 1] if i > 0 else None
    before = [l for l in landings if l[0] <= t]
    window = [l for l in before if prev is None or l[0] > prev]
    first = before[0] if before else None
    touches = ([first] if first is not None and prev is not None and first[0] <= prev else []) + window

    n = len(touches)
    credit: Dict[str, float] = defaultdict(float)
    for k, (_, channel) in enumerate(touches):
        credit[channel] += 1.0 if n == 1 else 0.5 if n == 2 else 0.4 if k in (0, n - 1) else 0.2 / (n - 2)

    if window:
        status = "attributed"
    else:
        status = "unattributed" if i == 0 else "returning_no_new_touch"
    return Stamp(
        status=status,
        conversion_type="first_order" if i == 0 else "repeat_order",
        first_touch=first[1] if first else "",
        last_touch=window[-1][1] if window else "",
        u_shaped=dict(credit),
        touches=n,
    )


def first_refresh(t: datetime) -> datetime:
    """Returns when the refresh that picks up something delivered at t runs."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    steps = -((epoch - t) // REFRESH_INTERVAL)  # ceiling division
    return epoch + steps * REFRESH_INTERVAL


def restamp_deadline(order_time: datetime) -> datetime:
    """Returns the time from which refreshes no longer re-stamp the order."""
    day = order_time.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(days=RESTAMP_DAYS)


def expected_orders(r: CustomerResult, seed: int) -> Dict[str, Expected]:
    """Returns the expected answer for every order the customer places."""
    delivered: Dict[object, datetime] = {}
    for d in r.deliveries:  # an event counts from its first delivery
        e = d.event.event_id
        delivered[e] = min(delivered.get(e, d.deliver_at), d.deliver_at)

    # A cookie is linked when the first event on it carrying a user_id arrives.
    linked_at: Dict[str, datetime] = {}
    for e in r.events:
        if e.user_id:
            when = delivered[e.event_id]
            linked_at[e.cookie_id] = min(linked_at.get(e.cookie_id, when), when)

    landing_events = sorted((e for e in r.events if e.is_landing), key=lambda e: (e.event_time, e.channel))
    all_landings = [(e.event_time, e.channel) for e in landing_events]
    visible = [(e.event_time, e.channel) for e in landing_events if e.cookie_id in linked_at]

    orders: Dict[str, Tuple[datetime, Decimal]] = {}
    for e in r.events:
        if e.order_id:
            orders.setdefault(e.order_id, (e.event_time, e.revenue))
    ordered = sorted(orders.items(), key=lambda o: o[1][0])
    order_times = [t for _, (t, _) in ordered]

    out: Dict[str, Expected] = {}
    for i, (order_id, (t, revenue)) in enumerate(ordered):
        deadline = restamp_deadline(t)
        in_time = lambda when: first_refresh(when) < deadline
        reachable_landings = [
            (e.event_time, e.channel) for e in landing_events
            if e.cookie_id in linked_at and in_time(delivered[e.event_id]) and in_time(linked_at[e.cookie_id])
        ]
        real_stamp = stamp(all_landings, order_times, i)
        visible_stamp = stamp(visible, order_times, i)
        reachable_stamp = stamp(reachable_landings, order_times, i)
        out[order_id] = Expected(
            order_id=order_id, user_id=r.customer.user_id, seed=seed, order_time=t, revenue=revenue,
            reachable=reachable_stamp, real=real_stamp,
            gap_device=not real_stamp.same_as(visible_stamp),
            gap_window=not visible_stamp.same_as(reachable_stamp),
        )
    return out
