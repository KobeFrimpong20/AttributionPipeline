"""The event row sent to attribution.events, plus seeded ID helpers."""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, Literal

Source = Literal["client", "server"]


@dataclass(frozen=True)
class Event:
    """One row for attribution.events. received_at is set at insert time."""

    event_id: uuid.UUID
    event_type: str
    event_time: datetime
    cookie_id: str
    user_id: str  # '' until the customer identifies themselves
    source: Source
    is_landing: bool
    channel: str  # set on landings only
    order_id: str = ""  # order_placed only
    revenue: Decimal = Decimal("0.00")  # order_placed only
    properties: Dict[str, str] = field(default_factory=dict)


def new_uuid(rng: random.Random) -> uuid.UUID:
    """Returns a UUID drawn from rng, so seeded runs are reproducible."""
    return uuid.UUID(int=rng.getrandbits(128), version=4)


def new_id(rng: random.Random, prefix: str) -> str:
    """Returns a readable ID such as 'cookie_3f2a9c...'."""
    return f"{prefix}_{new_uuid(rng).hex[:16]}"
