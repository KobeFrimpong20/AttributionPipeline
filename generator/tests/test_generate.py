import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from customers import simulate_customer
from delivery import Delivery
from events import Event
from explain import explain
from generate import (
    CUSTOMER_COLUMNS,
    EVENT_COLUMNS,
    TICK_SECONDS,
    Config,
    run_workers,
    to_row,
)

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


class JumpingClock(FakeClock):
    """A fake clock that jumps `gap` forward at `at`, as if the host slept."""

    def __init__(self, now, at, gap):
        super().__init__(now)
        self.at, self.gap, self.jumped = at, gap, False

    def sleep(self, seconds):
        super().sleep(seconds)
        if not self.jumped and self.now >= self.at:
            self.now += self.gap
            self.jumped = True


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
        self.assertEqual(len({col(c, "seed", CUSTOMER_COLUMNS) for c in sink.customers}), 30 * 60 * 0.5 + 1)
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


class LogTest(unittest.TestCase):
    def run_logged(self, clock, config, until):
        lines = []
        run_workers(0, config, clock, clock.sleep, ListSink(), until=until, log=lines.append)
        return lines

    def test_each_process_announces_itself(self):
        for process in range(2):
            clock = FakeClock(START)
            config = Config(arrivals_per_sec=1.0, processes=2)
            lines = []
            run_workers(process, config, clock, clock.sleep, ListSink(),
                        until=START + timedelta(seconds=5), log=lines.append)
            self.assertTrue(lines[0].startswith(f"process {process}/2 started"))

    def test_lag_and_waiting_trend(self):
        clock = FakeClock(START)
        config = Config(arrivals_per_sec=2.0)
        lines = self.run_logged(clock, config, START + timedelta(minutes=10))
        progress = [l for l in lines if "events/s" in l]
        self.assertTrue(progress)
        waiting = [int(l.split(" customers waiting")[0].split(", ")[-1].replace(",", "")) for l in progress]
        deltas = [int(l.split("customers waiting (")[1].split(")")[0].replace(",", "")) for l in progress]
        self.assertEqual(deltas, [b - a for a, b in zip([0] + waiting, waiting)])
        for l in progress:
            lag = float(l.split("max lag ")[1].split("s,")[0])
            self.assertLessEqual(lag, config.flush_seconds + TICK_SECONDS + 1e-6)


class StallTest(unittest.TestCase):
    def test_clock_jump_is_an_outage_not_a_backfill(self):
        jump_at = START + timedelta(minutes=10)
        gap = timedelta(hours=2)
        clock = JumpingClock(START, at=jump_at, gap=gap)
        sink = ListSink()
        lines = []
        until = jump_at + gap + timedelta(minutes=5)
        run_workers(0, Config(arrivals_per_sec=0.5), clock, clock.sleep, sink, until=until, log=lines.append)

        self.assertTrue(clock.jumped)
        warnings = [l for l in lines if "WARNING clock jumped 2h00m" in l]
        self.assertEqual(len(warnings), 1)
        starts = [col(r, "start", CUSTOMER_COLUMNS) for r in sink.customers]
        in_gap = [t for t in starts if jump_at + timedelta(seconds=1) < t < jump_at + gap]
        self.assertEqual(in_gap, [])
        self.assertTrue(any(t >= jump_at + gap for t in starts))
        # Customers active before the jump still send the events that fell in the gap.
        overdue = [r for r in sink.events if jump_at + timedelta(seconds=1) < col(r, "event_time") < jump_at + gap]
        self.assertTrue(overdue)
        self.assertTrue(all(col(r, "cookie_id") in {col(c, "cookie_id", CUSTOMER_COLUMNS) for c in sink.customers}
                            for r in overdue))


class ExplainTest(unittest.TestCase):
    def test_lists_every_delivery(self):
        r = simulate_customer(3, START)
        text = explain(3, START)
        for cookie in r.cookies:
            self.assertIn(cookie, text)
        for d in r.deliveries:
            self.assertIn(str(d.event.event_id), text)


if __name__ == "__main__":
    unittest.main()
