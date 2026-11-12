"""One process's scheduling state: arriving customers, sleeping workers, due deliveries.

`step` moves the state forward to `now` and returns what should be inserted. It never
reads the clock or sleeps, so tests drive it with any sequence of times.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Tuple

from delivery import Delivery
from workers import Worker, create_worker, next_visit

NEVER = datetime.max.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CustomerRecord:
    """One row for test.customers: links a cookie back to the input that made it."""

    cookie_id: str
    seed: int
    start: datetime
    late_sender: bool


@dataclass(frozen=True)
class StepResult:
    deliveries: List[Delivery]  # due by `now`, in deliver_at order
    customers: List[CustomerRecord]  # customers whose first visit started in this step


@dataclass
class Scheduler:
    seeds: Iterator[int]
    next_arrival: datetime
    # Heaps of (time, tie-break, item); the counter means items are never compared.
    waiting: List[Tuple[datetime, int, Worker]] = field(default_factory=list)
    due: List[Tuple[datetime, int, Delivery]] = field(default_factory=list)
    counter: Iterator[int] = field(default_factory=itertools.count)


def new_scheduler(seeds: Iterator[int], start: datetime) -> Scheduler:
    """Returns a scheduler whose first customer arrives at `start`."""
    return Scheduler(seeds=seeds, next_arrival=start)


def skip_arrivals(s: Scheduler, now: datetime, arrivals_per_sec: float) -> int:
    """Moves the next arrival past `now` without creating anyone; returns how many were skipped.

    Used after an outage: customers who would have arrived during it never do. The
    arrival spacing is kept.
    """
    if s.next_arrival > now:
        return 0
    interval = timedelta(seconds=1.0 / arrivals_per_sec)
    skipped = (now - s.next_arrival) // interval + 1
    s.next_arrival += skipped * interval
    return skipped


def step(s: Scheduler, now: datetime, arrivals_per_sec: float) -> StepResult:
    """Advances the scheduler to `now` and returns the deliveries that are due."""
    interval = timedelta(seconds=1.0 / arrivals_per_sec)
    while s.next_arrival <= now:
        seed = next(s.seeds, None)
        if seed is None:  # a finite seed list has run out: no more arrivals
            s.next_arrival = NEVER
            break
        worker = create_worker(seed, s.next_arrival)
        heapq.heappush(s.waiting, (worker.wake_at, next(s.counter), worker))
        s.next_arrival += interval

    customers: List[CustomerRecord] = []
    while s.waiting and s.waiting[0][0] <= now:
        _, _, worker = heapq.heappop(s.waiting)
        visit = next_visit(worker)
        if worker.visit == 0:
            customers.append(CustomerRecord(visit.cookie_id, worker.seed, worker.start, visit.late_sender))
        for d in visit.deliveries:
            heapq.heappush(s.due, (d.deliver_at, next(s.counter), d))
        if visit.next_worker is not None:
            heapq.heappush(s.waiting, (visit.next_worker.wake_at, next(s.counter), visit.next_worker))

    deliveries: List[Delivery] = []
    while s.due and s.due[0][0] <= now:
        deliveries.append(heapq.heappop(s.due)[2])
    return StepResult(deliveries=deliveries, customers=customers)
