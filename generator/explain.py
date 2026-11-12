"""Prints everything the generator injected for one customer.

Look up a cookie's seed and start time in test.customers, then:

    python -m explain --seed 1794460616000000007 --start '2026-11-12 05:16:58.350000'
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import List

from customers import simulate_customer
from workers import deliveries_by_visit


def explain(seed: int, start: datetime) -> str:
    r = simulate_customer(seed, start)
    lines: List[str] = [
        f"cookie {r.customer.cookie_id}  user {r.customer.user_id or '-'}  "
        f"late sender {'yes' if r.late_sender else 'no'}  visits {len(r.visits)}",
    ]
    for i, (summary, deliveries) in enumerate(zip(r.visits, deliveries_by_visit(r))):
        landing = next((d.event for d in deliveries if d.event.is_landing), None)
        touch = landing.channel if landing else "NO LANDING"
        lines.append(f"\nvisit {i + 1}: {touch}{'  ORDER' if summary.ordered else ''}")
        for d in deliveries:
            e = d.event
            delay = (d.deliver_at - e.event_time).total_seconds()
            flags = [f for f, on in (("held", d.held), ("duplicate", d.duplicate)) if on]
            lines.append(
                f"  {e.event_time:%H:%M:%S}  +{delay:7.1f}s  {e.source:6}  {e.event_type:26}"
                f"  {e.event_id}  {' '.join(flags)}"
            )
    return "\n".join(lines)


def parse_start(text: str) -> datetime:
    """Parses the start column as ClickHouse prints it; a missing offset means UTC."""
    start = datetime.fromisoformat(text.replace(" ", "T"))
    return start if start.tzinfo else start.replace(tzinfo=timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--start", type=parse_start, required=True,
                        help="the start column from test.customers, e.g. '2026-11-12 05:16:58.123456'")
    args = parser.parse_args()
    print(explain(args.seed, args.start))


if __name__ == "__main__":
    main()
