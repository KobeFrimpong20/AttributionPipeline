"""Prints summary statistics for simulated customers.

Usage (from generator/): python -m stats --customers 300000 --seed 1
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Tuple

from customers import MAX_VISITS, CustomerResult, simulate_customer


@dataclass(frozen=True)
class Summary:
    customers: int
    visits: int
    events: int
    first_time_visits: int
    first_time_orders: int
    repeat_visits: int  # visits by customers who had already ordered
    repeat_orders: int
    buyers: int
    repeat_buyers: int
    capped: int
    late_senders: int
    held_events: int
    bursts: int
    medium_bursts: int
    orders_before_landing: int  # server order delivered before its visit's landing


MEDIUM_DELAY = timedelta(minutes=15)


def delivery_counts(r: CustomerResult) -> Tuple[int, int, int, int]:
    """Returns (held events, bursts, medium bursts, orders delivered before their landing)."""
    held = [d for d in r.deliveries if d.held]
    bursts: Dict[datetime, datetime] = {}  # deliver_at -> last held event_time
    for d in held:
        bursts[d.deliver_at] = max(bursts.get(d.deliver_at, d.event.event_time), d.event.event_time)
    medium = sum(at - last >= MEDIUM_DELAY for at, last in bursts.items())

    early_orders = 0
    landing_at = datetime.min.replace(tzinfo=timezone.utc)
    for d in r.deliveries:  # events are in visit order, each visit opens with its landing
        if d.event.is_landing:
            landing_at = d.deliver_at
        elif d.event.source == "server" and d.deliver_at < landing_at:
            early_orders += 1
    return len(held), len(bursts), medium, early_orders


def summarise(results: Iterable[CustomerResult]) -> Summary:
    customers = visits = events = buyers = repeat_buyers = capped = 0
    late = held = bursts = medium = early = 0
    ft_visits = ft_orders = rp_visits = rp_orders = 0
    for r in results:
        customers += 1
        visits += len(r.visit_orders)
        events += len(r.events)
        has_ordered = False
        for ordered in r.visit_orders:
            if has_ordered:
                rp_visits += 1
                rp_orders += ordered
            else:
                ft_visits += 1
                ft_orders += ordered
            has_ordered = has_ordered or ordered
        orders = sum(r.visit_orders)
        buyers += orders > 0
        repeat_buyers += orders > 1
        capped += len(r.visit_orders) >= MAX_VISITS
        late += r.late_sender
        h, b, m, o = delivery_counts(r)
        held, bursts, medium, early = held + h, bursts + b, medium + m, early + o
    return Summary(customers, visits, events, ft_visits, ft_orders, rp_visits, rp_orders,
                   buyers, repeat_buyers, capped, late, held, bursts, medium, early)


def ratio(a: int, b: int) -> float:
    return a / b if b else 0.0


def format_summary(s: Summary) -> str:
    return "\n".join([
        f"customers                       {s.customers:,}",
        f"order rate per visit, new       {ratio(s.first_time_orders, s.first_time_visits):.2%}",
        f"order rate per visit, repeat    {ratio(s.repeat_orders, s.repeat_visits):.2%}",
        f"visits per customer             {ratio(s.visits, s.customers):.2f}",
        f"events per customer             {ratio(s.events, s.customers):.2f}",
        f"customers who order             {ratio(s.buyers, s.customers):.2%}",
        f"buyers who order 2+ times       {ratio(s.repeat_buyers, s.buyers):.2%}",
        f"customers hitting visit cap     {ratio(s.capped, s.customers):.3%}",
        f"late senders                    {ratio(s.late_senders, s.customers):.2%}",
        f"events held offline             {ratio(s.held_events, s.events):.2%}",
        f"bursts with a medium delay      {ratio(s.medium_bursts, s.bursts):.2%}",
        f"orders delivered before landing {ratio(s.orders_before_landing, s.first_time_orders + s.repeat_orders):.2%}",
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customers", type=int, default=100_000)
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    results = (simulate_customer(args.seed * 1_000_003 + i, start) for i in range(args.customers))
    print(format_summary(summarise(results)))


if __name__ == "__main__":
    main()
