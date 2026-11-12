"""Workers: one per customer, stored as a small cursor rather than a running task.

At 10k events/sec there are millions of customers between visits at any moment, so a
worker holds only what's needed to rebuild its customer: the seed, the start time and
the index of the next visit. When the worker wakes, the customer is re-simulated
(pure and cheap) and only that visit's deliveries are taken.

This is correct because every delivery of a visit lands before the next visit starts:
the longest delivery delay (about 20 min) is shorter than the shortest gap between
visits (31 min).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, List, Optional

from customers import CustomerResult, simulate_customer
from delivery import Delivery


@dataclass(frozen=True)
class Worker:
    wake_at: datetime  # when the next visit starts
    seed: int
    start: datetime  # when the customer arrived; with the seed, rebuilds every event
    visit: int  # index of the next visit


@dataclass(frozen=True)
class VisitStep:
    deliveries: List[Delivery]  # this visit's deliveries, duplicates included
    next_worker: Optional[Worker]  # None after the customer's last visit
    cookies: List[str]  # every cookie the customer uses, in order of first use
    late_sender: bool


def create_worker(seed: int, start: datetime) -> Worker:
    """Creates the worker for one customer. The seed decides everything it will do."""
    return Worker(wake_at=start, seed=seed, start=start, visit=0)


def deliveries_by_visit(r: CustomerResult) -> List[List[Delivery]]:
    """Groups a customer's deliveries by visit; a duplicate stays with its original."""
    starts = {v.first_event for v in r.visits}
    grouped: List[List[Delivery]] = []
    originals = 0
    for d in r.deliveries:
        if not d.duplicate:
            if originals in starts:
                grouped.append([])
            originals += 1
        grouped[-1].append(d)
    return grouped


def next_visit(worker: Worker) -> VisitStep:
    """Returns the worker's next visit and the worker for the visit after it."""
    r = simulate_customer(worker.seed, worker.start)
    visits = deliveries_by_visit(r)
    following = worker.visit + 1
    next_worker = None
    if following < len(r.visits):
        wake_at = r.events[r.visits[following].first_event].event_time
        next_worker = Worker(wake_at=wake_at, seed=worker.seed, start=worker.start, visit=following)
    return VisitStep(
        deliveries=visits[worker.visit],
        next_worker=next_worker,
        cookies=r.cookies,
        late_sender=r.late_sender,
    )


def seeds_for(process: int, processes: int, base: int = 0) -> Iterator[int]:
    """Returns process `process`'s seeds: base + process, then every `processes`-th seed.

    The processes' seed slices never overlap. A different `base` per run keeps cookie IDs
    from repeating across runs.
    """
    return itertools.count(base + process, processes)
