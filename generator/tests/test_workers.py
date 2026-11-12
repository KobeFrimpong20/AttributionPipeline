import itertools
import unittest
from datetime import datetime, timezone

from customers import simulate_customer
from workers import create_worker, deliveries_by_visit, next_visit, seeds_for

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def walk(seed):
    """Steps a worker through every visit and returns the steps and the workers."""
    worker = create_worker(seed, START)
    steps, workers = [], []
    while worker is not None:
        workers.append(worker)
        visit = next_visit(worker)
        steps.append(visit)
        worker = visit.next_worker
    return steps, workers


class WorkerTest(unittest.TestCase):
    def test_stepping_a_worker_gives_the_whole_customer(self):
        for seed in range(1_000):
            r = simulate_customer(seed, START)
            steps, _ = walk(seed)
            self.assertEqual(len(steps), len(r.visits))
            self.assertEqual([d for s in steps for d in s.deliveries], r.deliveries)
            for s in steps:
                self.assertEqual(s.cookies, r.cookies)
                self.assertEqual(s.late_sender, r.late_sender)

    def test_worker_wakes_at_the_start_of_each_visit(self):
        for seed in range(1_000):
            r = simulate_customer(seed, START)
            _, workers = walk(seed)
            self.assertEqual(workers[0].wake_at, START)
            for worker, summary in zip(workers, r.visits):
                self.assertEqual(worker.wake_at, r.events[summary.first_event].event_time)

    def test_deliveries_by_visit_keeps_duplicates_with_their_visit(self):
        for seed in range(1_000):
            r = simulate_customer(seed, START)
            grouped = deliveries_by_visit(r)
            self.assertEqual(len(grouped), len(r.visits))
            for visit in grouped:
                self.assertFalse(visit[0].duplicate)
                self.assertTrue(all(d.event.cookie_id == visit[0].event.cookie_id for d in visit))


class SeedsTest(unittest.TestCase):
    def test_process_slices_are_disjoint_and_complete(self):
        processes = 4
        slices = [set(itertools.islice(seeds_for(p, processes, base=1_000), 250)) for p in range(processes)]
        union = set().union(*slices)
        self.assertEqual(len(union), 1_000)
        self.assertEqual(union, set(range(1_000, 2_000)))


if __name__ == "__main__":
    unittest.main()
