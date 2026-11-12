import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone

from customers import simulate_customer
from scheduler import new_scheduler, step

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
RATE = 0.1  # one customer every 10 seconds
CUSTOMERS = 60


def key(d):
    return (d.event.event_id, d.deliver_at, d.duplicate)


def run(tick=timedelta(minutes=1)):
    """Drives a scheduler over a finite set of seeds until it has nothing left."""
    s = new_scheduler(iter(range(CUSTOMERS)), START)
    now = START
    batches, customers = [], []
    while True:
        result = step(s, now, RATE)
        batches.append((now, result.deliveries))
        customers.extend(result.customers)
        if not s.waiting and not s.due and now > START + timedelta(seconds=CUSTOMERS / RATE):
            return batches, customers
        now += tick


class SchedulerTest(unittest.TestCase):
    def test_emits_every_delivery_exactly_once(self):
        batches, _ = run()
        emitted = Counter(key(d) for _, ds in batches for d in ds)
        expected = Counter()
        for i in range(CUSTOMERS):
            start = START + timedelta(seconds=i / RATE)
            expected.update(key(d) for d in simulate_customer(i, start).deliveries)
        self.assertEqual(emitted, expected)

    def test_never_early_and_never_more_than_a_tick_late(self):
        tick = timedelta(minutes=1)
        batches, _ = run(tick)
        for now, deliveries in batches:
            times = [d.deliver_at for d in deliveries]
            self.assertEqual(times, sorted(times))
            for t in times:
                self.assertLessEqual(t, now)
                self.assertGreater(t, now - tick)

    def test_arrivals_follow_the_rate(self):
        _, customers = run()
        self.assertEqual([c.seed for c in customers], list(range(CUSTOMERS)))
        for i, c in enumerate(customers):
            self.assertEqual(c.start, START + timedelta(seconds=i / RATE))
            r = simulate_customer(c.seed, c.start)
            self.assertEqual(c.cookie_id, r.customer.cookie_id)
            self.assertEqual(c.late_sender, r.late_sender)

    def test_open_ended_seeds_keep_arriving(self):
        s = new_scheduler(iter(range(10**9)), START)
        result = step(s, START + timedelta(seconds=99), arrivals_per_sec=2.0)
        self.assertEqual(len(result.customers), 199)  # arrivals at 0, 0.5, ..., 99 s


if __name__ == "__main__":
    unittest.main()
