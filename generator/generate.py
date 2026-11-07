"""Streams simulated events into ClickHouse in batches at a steady rate.

Why batches: every INSERT creates at least one new on-disk part, which background
merges must later combine. A row-by-row insert would create parts far faster than
merges can handle, and ClickHouse would eventually reject writes with a "too many
parts" error. Each flush below is one batch INSERT.
"""

from __future__ import annotations

import heapq
import itertools
import os
import random
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, List, Mapping, Optional, Sequence, Tuple

import clickhouse_connect
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import OperationalError

from journeys import Event, build_journey

# received_at is deliberately missing: omitting it makes ClickHouse apply the column's
# DEFAULT now64(3), so the server records when each row was actually ingested.
INSERT_COLUMNS: Tuple[str, ...] = (
    "event_id", "event_time", "anonymous_id", "user_id", "session_id", "event_type",
    "source", "channel", "utm_campaign", "product_id", "order_id", "revenue",
)

SHARED_DEVICE_SHARE = 0.05  # chance a new customer is offered an earlier customer's device
SHARED_DEVICE_POOL_SIZE = 1_000
TICK_SECONDS = 0.1
# Converting a float time to a datetime and back can round it up by a fraction of a
# microsecond. Without this tolerance, a new customer's first event could land just
# after `now`, and take_due would keep starting customers while nothing became due.
# One millisecond is the resolution of the event_time column.
DUE_TOLERANCE_SECONDS = 0.001
LOG_EVERY_SECONDS = 10.0
CONNECT_ATTEMPTS = 30
CONNECT_RETRY_SECONDS = 2.0


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    user: str
    password: str
    database: str
    events_per_sec: int
    batch_size: int  # A batch is flushed once it reaches this many rows...
    flush_interval_sec: float  # ...or once this much time has passed, whichever is first.
    num_customers: int  # 0 means keep generating until stopped.
    seed: Optional[int]  # Set for a reproducible run.


def load_config(env: Mapping[str, str]) -> Config:
    """Builds the Config from environment variables."""
    seed = env.get("SEED", "")
    return Config(
        host=env["CH_HOST"],
        port=int(env.get("CH_PORT", "8123")),  # clickhouse-connect uses the HTTP interface.
        user=env["CH_USER"],
        password=env["CH_PASSWORD"],
        database=env["CH_DB"],
        events_per_sec=int(env.get("EVENTS_PER_SEC", "200")),
        batch_size=int(env.get("BATCH_SIZE", "10000")),
        flush_interval_sec=float(env.get("FLUSH_INTERVAL_SEC", "1")),
        num_customers=int(env.get("NUM_CUSTOMERS", "0")),
        seed=int(seed) if seed else None,
    )


class EventSource:
    """Holds events waiting to be delivered and starts new customers as needed."""

    def __init__(self, rng: random.Random, num_customers: int) -> None:
        self._rng = rng
        self._num_customers = num_customers
        self._customers_started = 0
        # Min-heap of (deliver_at, tie_breaker, event), so the next event due is first.
        # The tie-breaker stops heapq from comparing Event objects on equal times.
        self._pending: List[Tuple[float, int, Event]] = []
        self._sequence = itertools.count()
        self._shareable_devices: Deque[str] = deque(maxlen=SHARED_DEVICE_POOL_SIZE)

    @property
    def exhausted(self) -> bool:
        """True when there are no customers left to start and nothing is waiting."""
        return not self._pending and not self._can_start_customer()

    def _can_start_customer(self) -> bool:
        return self._num_customers == 0 or self._customers_started < self._num_customers

    def _start_customer(self, now: float) -> None:
        shared: Optional[str] = None
        if self._shareable_devices and self._rng.random() < SHARED_DEVICE_SHARE:
            shared = self._shareable_devices.popleft()  # Each device is shared only once.
        journey = build_journey(self._rng, now, shared)
        self._customers_started += 1
        if journey.shareable_anonymous_id is not None:
            self._shareable_devices.append(journey.shareable_anonymous_id)
        for delivery in journey.deliveries:
            heapq.heappush(
                self._pending, (delivery.deliver_at, next(self._sequence), delivery.event)
            )

    def take_due(self, now: float, limit: int) -> List[Event]:
        """Returns up to `limit` events, starting new customers if too few are due."""
        events: List[Event] = []
        while len(events) < limit:
            if self._pending and self._pending[0][0] <= now + DUE_TOLERANCE_SECONDS:
                events.append(heapq.heappop(self._pending)[2])
            elif self._can_start_customer():
                self._start_customer(now)
            else:
                break
        return events


def to_row(event: Event) -> List[Any]:
    """Converts an Event to a list of values in INSERT_COLUMNS order."""
    return [getattr(event, column) for column in INSERT_COLUMNS]


def split_batches(events: Sequence[Event], batch_size: int) -> List[Sequence[Event]]:
    """Splits events into slices of at most batch_size."""
    return [events[i : i + batch_size] for i in range(0, len(events), batch_size)]


def connect(config: Config) -> Client:
    """Connects to ClickHouse, retrying until the server accepts remote connections.

    The healthcheck can pass before this succeeds. While init scripts run, the image
    runs a temporary server that listens only on 127.0.0.1 inside its own container.
    """
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return clickhouse_connect.get_client(
                host=config.host,
                port=config.port,
                username=config.user,
                password=config.password,
                database=config.database,
            )
        except OperationalError as error:
            if attempt == CONNECT_ATTEMPTS:
                raise
            print(f"ClickHouse not ready (attempt {attempt}/{CONNECT_ATTEMPTS}): {error}")
            time.sleep(CONNECT_RETRY_SECONDS)
    raise AssertionError("unreachable: the final attempt returns or raises")


def insert_events(client: Client, events: Sequence[Event]) -> None:
    """Sends one batch INSERT. It also runs the materialized views on these rows."""
    client.insert("events", [to_row(event) for event in events], column_names=INSERT_COLUMNS)


def run(client: Client, source: EventSource, config: Config, stop: threading.Event) -> None:
    """Paces events at EVENTS_PER_SEC and flushes them on the batch size or interval."""
    started_at = time.monotonic()
    last_flush = started_at
    last_log = started_at
    emitted = 0
    inserted = 0
    buffer: List[Event] = []

    def flush() -> None:
        nonlocal buffer, inserted, last_flush
        for batch in split_batches(buffer, config.batch_size):
            insert_events(client, batch)
            inserted += len(batch)
        buffer = []
        last_flush = time.monotonic()

    while not stop.is_set() and not source.exhausted:
        now = time.monotonic()
        # Work out how many events should exist by now, so pacing does not drift.
        owed = int((now - started_at) * config.events_per_sec) - emitted
        events = source.take_due(time.time(), owed)
        emitted += len(events)
        buffer.extend(events)

        if len(buffer) >= config.batch_size or now - last_flush >= config.flush_interval_sec:
            flush()
        if now - last_log >= LOG_EVERY_SECONDS:
            print(f"inserted {inserted} events ({inserted / (now - started_at):.0f}/sec)")
            last_log = now
        stop.wait(TICK_SECONDS)

    flush()
    print(f"finished: inserted {inserted} events")


def main() -> None:
    config = load_config(os.environ)
    stop = threading.Event()
    # `docker compose stop` sends SIGTERM. Stop cleanly so the last batch is written.
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    client = connect(config)
    print(
        f"connected to {config.host}:{config.port}; generating {config.events_per_sec} events/sec"
    )
    run(client, EventSource(random.Random(config.seed), config.num_customers), config, stop)


if __name__ == "__main__":
    main()
