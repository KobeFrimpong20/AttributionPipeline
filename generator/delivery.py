"""Decides when each event reaches the pipeline (late events).

The journey model decides what happened and when (`event_time`). This module decides
when each event is delivered (`deliver_at`), which becomes `received_at` on insert.

Most customers send on time. A late sender has a flaky connection: in each visit the
connection drops at some client event, the app holds that event and every later
client event, and flushes them all together once it reconnects. Server events are
never held, so a held landing can arrive after the server's order event.

Any delivery can also be sent twice: a client retry after a network blip, or two
server processes racing. The copy is the same event (same event_id and event_time)
delivered a few seconds later.

Everything here is pure; randomness comes from the `rng` passed in.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import List, Sequence

from events import Event

LATE_SENDER_SHARE = 0.09
# Short delays stay inside the refresh overlap; medium ones span several refreshes.
MEDIUM_DELAY_SHARE = 0.15
SHORT_DELAY_SECONDS = (5.0, 3 * 60.0)
MEDIUM_DELAY_SECONDS = (15 * 60.0, 20 * 60.0)

DUPLICATE_SHARE = 0.05
DUPLICATE_GAP_SECONDS = (0.1, 3.0)


@dataclass(frozen=True)
class Delivery:
    event: Event
    deliver_at: datetime
    held: bool  # part of an offline burst
    duplicate: bool = False  # second copy of an event already delivered


def delivery_rng(seed: int) -> random.Random:
    """Returns the customer's delivery RNG, separate from their journey RNG.

    A separate stream means delivery draws never change the journey a seed produces.
    """
    return random.Random(f"{seed}:delivery")


def duplicates_rng(seed: int) -> random.Random:
    """Returns the customer's duplicate-send RNG, separate from the other streams."""
    return random.Random(f"{seed}:duplicates")


def is_late_sender(rng: random.Random) -> bool:
    return rng.random() < LATE_SENDER_SHARE


def draw_delay(rng: random.Random) -> timedelta:
    """Returns how long after the last held event the burst is sent."""
    bounds = MEDIUM_DELAY_SECONDS if rng.random() < MEDIUM_DELAY_SHARE else SHORT_DELAY_SECONDS
    return timedelta(seconds=rng.uniform(*bounds))


def on_time(event: Event) -> Delivery:
    return Delivery(event=event, deliver_at=event.event_time, held=False)


def deliver_visit(rng: random.Random, events: Sequence[Event], late_sender: bool) -> List[Delivery]:
    """Returns one delivery per event of a visit, in the same order as `events`."""
    client = [i for i, e in enumerate(events) if e.source == "client"]
    if not late_sender or not client:
        return [on_time(e) for e in events]

    drop = rng.choice(client)  # the first client event that is held
    held = {i for i in client if i >= drop}
    last_held = max(events[i].event_time for i in held)
    deliver_at = last_held + draw_delay(rng)
    return [
        Delivery(event=e, deliver_at=deliver_at, held=True) if i in held else on_time(e)
        for i, e in enumerate(events)
    ]


def add_duplicates(rng: random.Random, deliveries: Sequence[Delivery]) -> List[Delivery]:
    """Returns every delivery, each followed by a copy with probability DUPLICATE_SHARE.

    The copy carries the same Event, so its event_id matches the original's.
    """
    out: List[Delivery] = []
    for d in deliveries:
        out.append(d)
        if rng.random() < DUPLICATE_SHARE:
            gap = timedelta(seconds=rng.uniform(*DUPLICATE_GAP_SECONDS))
            out.append(replace(d, deliver_at=d.deliver_at + gap, duplicate=True))
    return out
