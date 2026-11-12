import random
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest import mock

import delivery
from customers import simulate_customer
from delivery import (
    DUPLICATE_GAP_SECONDS,
    MEDIUM_DELAY_SECONDS,
    SHORT_DELAY_SECONDS,
    add_duplicates,
    deliver_visit,
    draw_delay,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
MIN_DELAY = timedelta(seconds=SHORT_DELAY_SECONDS[0])
MAX_DELAY = timedelta(seconds=MEDIUM_DELAY_SECONDS[1])


def originals(deliveries):
    return [d for d in deliveries if not d.duplicate]


def split_visits(deliveries):
    """Groups a customer's original deliveries by visit; each visit opens with its landing."""
    visits = []
    for d in originals(deliveries):
        if d.event.is_landing:
            visits.append([])
        visits[-1].append(d)
    return visits


def late_customers(n=3_000):
    for seed in range(n):
        r = simulate_customer(seed, START)
        if r.late_sender:
            yield r


class DeliverVisitTest(unittest.TestCase):
    def test_on_time_sender_delivers_at_event_time(self):
        events = simulate_customer(7, START).events
        out = deliver_visit(random.Random(0), events, late_sender=False)
        self.assertEqual([d.event for d in out], list(events))
        for d in out:
            self.assertFalse(d.held)
            self.assertEqual(d.deliver_at, d.event.event_time)

    def test_held_events_are_one_burst_at_the_end_of_the_visit(self):
        for r in late_customers():
            for visit in split_visits(r.deliveries):
                client = [d for d in visit if d.event.source == "client"]
                flags = [d.held for d in client]
                first = flags.index(True)
                # A contiguous run of client events through the end of the visit.
                self.assertTrue(all(flags[first:]))
                self.assertFalse(any(flags[:first]))
                held = client[first:]
                self.assertEqual(len({d.deliver_at for d in held}), 1)
                delay = held[0].deliver_at - max(d.event.event_time for d in held)
                self.assertGreaterEqual(delay, MIN_DELAY)
                self.assertLessEqual(delay, MAX_DELAY)
                for d in client[:first]:
                    self.assertEqual(d.deliver_at, d.event.event_time)

    def test_server_events_are_never_held(self):
        for r in late_customers():
            for d in originals(r.deliveries):
                if d.event.source == "server":
                    self.assertFalse(d.held)
                    self.assertEqual(d.deliver_at, d.event.event_time)

    def test_burst_arrives_before_the_next_visit(self):
        for r in late_customers():
            visits = split_visits(r.deliveries)
            for current, nxt in zip(visits, visits[1:]):
                last_delivery = max(d.deliver_at for d in current)
                self.assertLess(last_delivery, nxt[0].event.event_time)


class DuplicatesTest(unittest.TestCase):
    MIN_GAP = timedelta(seconds=DUPLICATE_GAP_SECONDS[0])
    MAX_GAP = timedelta(seconds=DUPLICATE_GAP_SECONDS[1])

    def test_duplicate_follows_its_original(self):
        for seed in range(3_000):
            deliveries = simulate_customer(seed, START).deliveries
            self.assertFalse(deliveries[0].duplicate)
            for prev, d in zip(deliveries, deliveries[1:]):
                if not d.duplicate:
                    continue
                self.assertFalse(prev.duplicate)
                self.assertIs(d.event, prev.event)
                self.assertEqual(d.held, prev.held)
                self.assertGreaterEqual(d.deliver_at - prev.deliver_at, self.MIN_GAP)
                self.assertLessEqual(d.deliver_at - prev.deliver_at, self.MAX_GAP)

    def test_every_event_sent_once_or_twice(self):
        for seed in range(3_000):
            r = simulate_customer(seed, START)
            self.assertEqual([d.event for d in originals(r.deliveries)], r.events)
            counts = Counter(d.event.event_id for d in r.deliveries)
            self.assertLessEqual(max(counts.values()), 2)

    def test_removing_duplicates_gives_the_plain_deliveries(self):
        events = simulate_customer(7, START).events
        plain = deliver_visit(random.Random(3), events, late_sender=True)
        self.assertEqual(originals(add_duplicates(random.Random(5), plain)), plain)

    def test_duplicate_share_and_sources(self):
        total = dups = 0
        sources = set()
        for seed in range(10_000):
            for d in simulate_customer(seed, START).deliveries:
                if d.duplicate:
                    dups += 1
                    sources.add(d.event.source)
                else:
                    total += 1
        self.assertAlmostEqual(dups / total, 0.05, delta=0.004)
        self.assertEqual(sources, {"client", "server"})


class SharesTest(unittest.TestCase):
    def test_late_sender_share(self):
        n = 20_000
        late = sum(delivery.is_late_sender(delivery.delivery_rng(s)) for s in range(n))
        self.assertAlmostEqual(late / n, 0.09, delta=0.008)

    def test_medium_delay_share(self):
        rng = random.Random(1)
        n = 20_000
        medium = sum(draw_delay(rng) >= timedelta(seconds=MEDIUM_DELAY_SECONDS[0]) for _ in range(n))
        self.assertAlmostEqual(medium / n, 0.15, delta=0.01)


class DeterminismTest(unittest.TestCase):
    def test_same_seed_same_deliveries(self):
        for seed in range(200):
            self.assertEqual(simulate_customer(seed, START).deliveries,
                             simulate_customer(seed, START).deliveries)

    def test_delivery_never_changes_the_journey(self):
        for seed in range(200):
            baseline = simulate_customer(seed, START).events
            with mock.patch.multiple(delivery, LATE_SENDER_SHARE=1.0, DUPLICATE_SHARE=1.0):
                forced = simulate_customer(seed, START)
            self.assertTrue(forced.late_sender)
            self.assertEqual(len(forced.deliveries), 2 * len(baseline))
            self.assertEqual(forced.events, baseline)


if __name__ == "__main__":
    unittest.main()
