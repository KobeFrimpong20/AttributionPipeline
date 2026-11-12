import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from customers import simulate_customer
from delivery import Delivery
from events import Event
from explain import explain
from generate import CUSTOMER_COLUMNS, EVENT_COLUMNS, Config, run_workers, to_row

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(self.now)
        self.now += timedelta(seconds=seconds)


class ListSink:
    def __init__(self):
        self.events = []
        self.customers = []
        self.batches = 0

    def insert_events(self, rows):
        self.events.extend(rows)
        self.batches += 1

    def insert_customers(self, rows):
        self.customers.extend(rows)


def col(row, name, columns=EVENT_COLUMNS):
    return row[columns.index(name)]


class ToRowTest(unittest.TestCase):
    def test_maps_every_column(self):
        event = Event(
            event_id=uuid.UUID(int=1), event_type="order_placed", event_time=START,
            cookie_id="cookie_1", user_id="user_1", source="server", is_landing=False,
            channel="", order_id="order_1", revenue=Decimal("12.50"), properties={"email": "a@b"},
        )
        row = to_row(Delivery(event=event, deliver_at=START, held=False))
        self.assertEqual(len(row), len(EVENT_COLUMNS))
        self.assertNotIn("received_at", EVENT_COLUMNS)
        self.assertEqual(col(row, "event_id"), uuid.UUID(int=1))
        self.assertEqual(col(row, "source"), "server")
        self.assertEqual(col(row, "revenue"), Decimal("12.50"))
        self.assertEqual(col(row, "properties"), {"email": "a@b"})


class RunWorkersTest(unittest.TestCase):
    def test_real_time_inserts_only_what_is_due(self):
        clock = FakeClock(START)
        sink = ListSink()
        until = START + timedelta(minutes=30)
        run_workers(0, Config(arrivals_per_sec=0.5), clock, clock.sleep, sink, until=until, log=lambda _: None)
        self.assertTrue(sink.events)
        self.assertEqual(len(sink.customers), 30 * 60 * 0.5 + 1)
        for row in sink.events:
            self.assertLessEqual(col(row, "event_time"), clock.now)
        self.assertTrue(clock.sleeps)

    def test_backfill_catches_up_without_sleeping(self):
        clock = FakeClock(START)
        sink = ListSink()
        config = Config(arrivals_per_sec=0.5, backfill_hours=1.0)
        run_workers(0, config, clock, clock.sleep, sink, until=START + timedelta(minutes=5), log=lambda _: None)
        # The past hour is built before the first sleep, so no sleep happens before START.
        self.assertGreaterEqual(clock.sleeps[0], START)
        backfilled = [r for r in sink.events if col(r, "event_time") < START]
        self.assertTrue(backfilled)
        earliest = min(col(r, "start", CUSTOMER_COLUMNS) for r in sink.customers)
        self.assertEqual(earliest, START - timedelta(hours=1))

    def test_process_seed_slices_and_run_ids_differ(self):
        def seeds(process, processes, run_id):
            clock = FakeClock(START)
            sink = ListSink()
            config = Config(arrivals_per_sec=1.0, processes=processes, run_id=run_id)
            run_workers(process, config, clock, clock.sleep, sink,
                        until=START + timedelta(seconds=20), log=lambda _: None)
            return {col(r, "seed", CUSTOMER_COLUMNS) for r in sink.customers}

        a, b = seeds(0, 2, run_id=7), seeds(1, 2, run_id=7)
        self.assertTrue(a and b)
        self.assertFalse(a & b)
        self.assertFalse(a & seeds(0, 2, run_id=8))


class ExplainTest(unittest.TestCase):
    def test_lists_every_delivery(self):
        r = simulate_customer(3, START)
        text = explain(3, START)
        self.assertIn(r.customer.cookie_id, text)
        for d in r.deliveries:
            self.assertIn(str(d.event.event_id), text)


if __name__ == "__main__":
    unittest.main()
