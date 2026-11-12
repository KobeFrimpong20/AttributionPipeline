# AttributionPipeline

A single-node ClickHouse pipeline for learning batch aggregation through marketing
attribution. A Python generator simulates e-commerce customer journeys and streams the
events into ClickHouse. Refreshable views then turn the raw events into sessions,
customer profiles and attributed orders every 5 minutes.

Each order gets three attribution models:

- **First-touch:** the channel that first brought the customer in.
- **Last-touch:** the latest channel the customer landed from before the order.
- **U-shaped:** 40% to the first touch, 40% to the last and 20% split across the touches
  in between.

The generator also records the correct answer for every order. A single verification
query checks the pipeline's output against it, including under messy data: late events,
duplicate sends, repeat customers, anonymous sessions before checkout and orders with no
landings. The target is 10,000 events/sec sustained end to end.

The full design is in the [design spec](PRD.md).

## How it works

```text
journey model ──> generator ──batch INSERT──> events (raw, append-only)
                      │                          │
                      │                every 5 min (refreshable views)
                      │                          ▼
                      │              sessions ─> profiles ─> conversions
                      │                                          │
                      └──> test.expected_attribution <── verification query
```

1. **Journey model:** a state machine (landing → product view → add to cart → order)
   with probabilistic transitions.
2. **Generator:** runs one seeded worker per customer, injects edge cases, batch-inserts
   into `events` and writes the ground truth to `test.expected_attribution`.
3. **`events`:** the raw event log. Duplicates are kept here and removed during refreshes.
4. **Refreshable views:** every 5 minutes they build `sessions`, merge them into
   `profiles` and stamp `conversions` with all three attribution models.
5. **Verification:** a full outer join of `conversions` against the ground truth that
   flags wrong, missing and invented orders.

## Status

The project is being rebuilt to match the current spec.

| # | Milestone | Status |
| --- | --- | --- |
| 1 | Docker Compose up, `events` table created, generator inserting | Done |
| 2 | Sessions refresh with deduplication and the 30-minute timeout | Done |
| 3 | Profiles keyed by cookie, with first touch | Not started |
| 4 | Conversions stamped with all three models | Not started |
| 5 | Ground-truth table and verification query passing | Not started |
| 6 | Load test at 10,000 events/sec, with results in this README | Not started |

## Run it

Requires Docker with Compose.

```sh
cp .env.example .env   # first run only
docker compose up -d   # starts ClickHouse
```

The generator runs behind a Compose profile:

```sh
docker compose --profile generator up --build
```

### Generator settings

Set these in `.env` or on the command line, e.g. `ARRIVALS_PER_SEC=100 docker compose --profile generator up --build`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARRIVALS_PER_SEC` | 20 | New customers per second, across all processes |
| `PROCESSES` | 1 | Generator processes; each runs its own slice of customers |
| `BATCH_SIZE` | 10000 | Rows per insert, at most |
| `FLUSH_SECONDS` | 1 | Insert at least this often |
| `BACKFILL_HOURS` | 0 | Hours of history to build at startup before running in real time |

**Why the setting is arrivals, not events per second.** Every event is inserted in real
time, when it happens. A customer sends about 8.4 events (duplicates included), so the
steady-state rate is about `8.4 × ARRIVALS_PER_SEC`, and 10,000 events/sec needs about
1,190 arrivals per second. Getting there takes a long time. The average customer is
active for about 2.5 hours, but repeat buyers keep coming back for much longer:

| Time since startup | 1 h | 3 h | 8 h | 12 h | 24 h |
| --- | --- | --- | --- | --- | --- |
| Event rate, share of steady state | 45% | 66% | 88% | 94% | about 100% |

**Backfill trade-off.** `BACKFILL_HOURS` skips the warm-up by building that much history
immediately (12 to 24 hours gets close to steady state). Backfilled events keep their real
`event_time`, but `received_at` is the insert time, so every one of them looks late. That
is fine for a demo, and for a load test that measures only after the backfill finishes.
For the verification run, warm up in real time instead.

### Reading the logs

`docker compose logs -f generator` shows one line per process every 10 seconds:

```
process 0/1: 155 events/s, 145,065 customers waiting (+12), 18,565 deliveries queued, max lag 0.9s, real time, simulated time 13:27:36
```

| Field | Meaning | Normal |
| --- | --- | --- |
| `events/s` | Rows inserted by this process over the last 10 s, duplicates included | Climbs along the warm-up table above |
| `customers waiting (+N)` | Customers between visits who will come back, and the change since the last line | Grows fast at first, then slows; levels off at about `8,800 × ARRIVALS_PER_SEC` after about a day. A shrinking `+N` means warm-up is ending, not a leak |
| `deliveries queued` | Events already simulated whose delivery time hasn't arrived (visits in progress, held late bursts) | Proportional to `events/s`, about 2 minutes' worth |
| `max lag` | Largest delay between an event's delivery time and its insert, over the last 10 s | About `FLUSH_SECONDS`. If it keeps growing, this process can't keep up: add `PROCESSES` |
| `real time` / `backfill` | Whether the process follows the clock or is still building backfill history | `backfill` only at startup, when `BACKFILL_HOURS > 0` |

Each process also prints `process p/N started` when it begins, so with `PROCESSES=4` you
should see four of them.

### Long runs on a laptop

Docker pauses while the Mac sleeps. When it wakes, the generator logs
`WARNING clock jumped ...` and treats the gap as an outage: customers who would have
arrived during it are skipped, and customers already active send their overdue events
late, in one burst. To avoid this during long runs, keep the Mac awake in another
terminal, for example with `caffeinate -i docker compose logs -f generator`. Restarting
the generator (for example to change `PROCESSES`) starts a new run: customers in memory
are dropped, and new ones start with a new `run_id`.

**How it scales.** A worker is one customer, but it is stored as a few fields (seed,
start time, next visit) in a heap rather than as a thread. When a visit is due, the
customer is re-simulated from its seed and that visit's events are queued for delivery.
At 10k events/sec about 10 million customers are between visits at any moment, which
this keeps to a few hundred bytes each.

ClickHouse runs the scripts in `clickhouse/init/` the first time it starts with an empty
data volume. If you change the schema, wipe the volume so the scripts run again. This
deletes all stored data:

```sh
docker compose down -v
```

## Explore the data

Open a ClickHouse shell:

```sh
docker compose exec clickhouse clickhouse-client
```

The event count should climb while the generator is running:

```sql
SELECT count() FROM attribution.events;
```

### Trace an event back to the generator

`attribution.events` holds only what production would see. The generator also writes one
row per customer to `test.customers` (`cookie_id`, `seed`, `start`, `late_sender`), and a
customer's seed and start time rebuild every event it sent. To see what was injected for
a cookie:

```sql
SELECT seed, start FROM test.customers WHERE cookie_id = 'cookie_...';
```

```sh
cd generator
python3 -m explain --seed <seed> --start '<start>'
```

This prints every visit (channel, or `NO LANDING`) and every delivery, with its delay and
`held` or `duplicate` flags. Edge cases can also be found directly in SQL:

```sql
-- duplicate sends
SELECT event_id, count() FROM attribution.events GROUP BY event_id HAVING count() > 1;
-- late events
SELECT * FROM attribution.events WHERE received_at - event_time > 60;
```

### Sessions

`attribution.mv_sessions` refreshes every 5 minutes (on the clock: :00, :05, ...). Check its
runs with:

```sql
SELECT status, last_success_time, read_rows, written_rows, exception
FROM system.view_refreshes WHERE view = 'mv_sessions';
```

`sessions` stores partial aggregate states, so a session can have several rows until
background merges combine them. Always read it with `GROUP BY` and the `-Merge` functions:

```sql
SELECT cookie_id, session_start,
       min(first_event) AS first_event, max(last_event) AS last_event,
       length(groupUniqArrayMerge(event_ids)) AS events,
       groupUniqArrayIfMerge(landings) AS landings,
       groupUniqArrayIfMerge(orders) AS orders,
       argMaxIfMerge(user_id) AS user_id
FROM attribution.sessions
WHERE cookie_id = 'cookie_...'
GROUP BY cookie_id, session_start ORDER BY first_event;
```

Use `first_event` for timing. `session_start` is only the key: the start as first seen,
which stays put when a late event from before it arrives.

Queries for profiles, conversions and the verification check will be added here as each
milestone lands.

## Known limitations

- **Split sessions:** sessions can be extended but not merged, so a late event that falls
  in the gap between two stored sessions leaves the visit split in two. Attribution is
  unaffected, but session counts may be slightly high. The generator never produces this,
  because its visits are at least 31 minutes apart.
- **One cookie is one customer:** profiles are keyed by cookie, with no identity
  resolution. Journeys across devices are not joined, which biases attribution towards
  late-funnel channels. Shared devices are not simulated.
- **Re-stamping window:** late landings only update orders in the last two daily
  partitions.
- **Batch, not real time:** numbers lag by up to about 5 minutes.

## Out of scope

Sub-second freshness, cross-device identity graphs, subscriptions, Kafka, clusters and
replication. Some of these are planned extensions (see the spec).
