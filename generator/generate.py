"""Streams simulated customers into ClickHouse in real time.

Each process runs one scheduling loop over its own slice of seeds: customers arrive at
a steady rate, every event is inserted once its `deliver_at` arrives, and inserts are
batched. `received_at` is left to the server, so it is the real insert time.

Usage: python -u generate.py  (configured with environment variables, see `main`)
"""

from __future__ import annotations

import multiprocessing
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Protocol, Tuple

from delivery import Delivery
from scheduler import CustomerRecord, new_scheduler, step
from workers import seeds_for

EVENT_COLUMNS: Tuple[str, ...] = (
    "event_id", "event_type", "event_time", "cookie_id", "user_id", "source",
    "is_landing", "channel", "order_id", "revenue", "properties",
)
CUSTOMER_COLUMNS: Tuple[str, ...] = ("cookie_id", "seed", "start", "late_sender")

TICK_SECONDS = 0.1  # how often the real-time loop wakes up
BACKFILL_STEP = timedelta(seconds=1)  # how far simulated time moves per backfill step
LOG_EVERY_SECONDS = 10.0
SEEDS_PER_RUN = 10**9  # each run's seeds start at run_id * SEEDS_PER_RUN

Clock = Callable[[], datetime]


class Sink(Protocol):
    def insert_events(self, rows: List[tuple]) -> None: ...
    def insert_customers(self, rows: List[tuple]) -> None: ...


@dataclass(frozen=True)
class Config:
    arrivals_per_sec: float  # new customers per second, across all processes
    processes: int = 1
    batch_size: int = 10_000
    flush_seconds: float = 1.0
    backfill_hours: float = 0.0
    run_id: int = 0  # keeps seeds, and so cookie IDs, distinct between runs


def to_row(d: Delivery) -> tuple:
    """Returns the values for EVENT_COLUMNS."""
    e = d.event
    return (
        e.event_id, e.event_type, e.event_time, e.cookie_id, e.user_id, e.source,
        e.is_landing, e.channel, e.order_id, e.revenue, dict(e.properties),
    )


def customer_row(c: CustomerRecord) -> tuple:
    """Returns the values for CUSTOMER_COLUMNS."""
    return (c.cookie_id, c.seed, c.start, c.late_sender)


def run_workers(
    process: int,
    config: Config,
    clock: Clock,
    sleep: Callable[[float], None],
    sink: Sink,
    until: Optional[datetime] = None,
    log: Callable[[str], None] = print,
) -> None:
    """Runs one process's loop. Stops at simulated time `until`, or never if it's None.

    With backfill, simulated time starts `backfill_hours` in the past and moves in
    BACKFILL_STEP steps without sleeping until it catches up with the clock. From then
    on it follows the clock.
    """
    rate = config.arrivals_per_sec / config.processes
    start = clock() - timedelta(hours=config.backfill_hours)
    # Offset each process's first arrival so arrivals interleave across processes.
    first_arrival = start + timedelta(seconds=process / config.arrivals_per_sec)
    seeds = seeds_for(process, config.processes, base=config.run_id * SEEDS_PER_RUN)
    sched = new_scheduler(seeds, first_arrival)

    events: List[tuple] = []
    customers: List[tuple] = []
    sim_now = start
    last_flush = last_log = clock()
    inserted = 0

    def flush() -> None:
        nonlocal events, customers, inserted
        if customers:
            sink.insert_customers(customers)
        if events:
            sink.insert_events(events)
        inserted += len(events)
        events, customers = [], []

    while until is None or sim_now < until:
        real = clock()
        backfilling = sim_now + BACKFILL_STEP < real
        sim_now = sim_now + BACKFILL_STEP if backfilling else real
        result = step(sched, sim_now, rate)
        events.extend(to_row(d) for d in result.deliveries)
        customers.extend(customer_row(c) for c in result.customers)

        if len(events) >= config.batch_size or (real - last_flush).total_seconds() >= config.flush_seconds:
            flush()
            last_flush = real
        if (real - last_log).total_seconds() >= LOG_EVERY_SECONDS:
            elapsed = (real - last_log).total_seconds()
            mode = "backfill" if backfilling else "real time"
            log(f"process {process}: {inserted / elapsed:,.0f} events/s, "
                f"{len(sched.waiting):,} customers waiting, {len(sched.due):,} deliveries queued, "
                f"{mode}, simulated time {sim_now:%H:%M:%S}")
            inserted, last_log = 0, real
        if not backfilling:
            sleep(TICK_SECONDS)
    flush()


class ClickHouseSink:
    """Inserts into attribution.events and test.customers."""

    def __init__(self) -> None:
        import clickhouse_connect  # imported here so tests don't need the driver

        self.client = clickhouse_connect.get_client(
            host=os.environ.get("CH_HOST", "localhost"),
            port=int(os.environ.get("CH_PORT", "8123")),
            username=os.environ.get("CH_USER", "default"),
            password=os.environ.get("CH_PASSWORD", ""),
            database=os.environ.get("CH_DB", "attribution"),
        )

    def insert_events(self, rows: List[tuple]) -> None:
        self.client.insert("events", rows, column_names=list(EVENT_COLUMNS))

    def insert_customers(self, rows: List[tuple]) -> None:
        self.client.insert("customers", rows, column_names=list(CUSTOMER_COLUMNS), database="test")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def process_main(process: int, config: Config) -> None:
    run_workers(process, config, utc_now, time.sleep, ClickHouseSink())


def generate_events(config: Config) -> None:
    """Runs one loop per process, each over its own slice of seeds, until stopped."""
    if config.processes == 1:
        process_main(0, config)
        return
    procs = [multiprocessing.Process(target=process_main, args=(p, config), daemon=True)
             for p in range(config.processes)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()


def config_from_env() -> Config:
    return Config(
        arrivals_per_sec=float(os.environ.get("ARRIVALS_PER_SEC", "20")),
        processes=int(os.environ.get("PROCESSES", "1")),
        batch_size=int(os.environ.get("BATCH_SIZE", "10000")),
        flush_seconds=float(os.environ.get("FLUSH_SECONDS", "1")),
        backfill_hours=float(os.environ.get("BACKFILL_HOURS", "0")),
        run_id=int(os.environ.get("RUN_ID") or time.time()),
    )


def main() -> None:
    config = config_from_env()
    print(f"generator starting: {config}")
    generate_events(config)


if __name__ == "__main__":
    main()
