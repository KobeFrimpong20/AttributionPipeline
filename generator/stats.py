"""Prints summary statistics for simulated customers.

Usage (from generator/): python -m stats --customers 300000 --seed 1
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

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


def summarise(results: Iterable[CustomerResult]) -> Summary:
    customers = visits = events = buyers = repeat_buyers = capped = 0
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
    return Summary(customers, visits, events, ft_visits, ft_orders, rp_visits, rp_orders,
                   buyers, repeat_buyers, capped)


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
