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
duplicate sends, repeat customers, anonymous sessions before checkout, orders with no
landings, cleared cookies and second devices. The target is 10,000 events/sec sustained
end to end.

The full design is in the [design spec](PRD.md).

## How it works

```text
journey model ──> generator ──batch INSERT──> events (raw, append-only)
                      │                          │
                      │                every 5 min (refreshable views)
                      │                          ▼
                      │     sessions ─> identity_links ─> profiles ─> conversions
                      │                                          │
                      └──> test.expected_attribution <── verification query
```

1. **Journey model:** a state machine (landing → product view → add to cart → order)
   with probabilistic transitions.
2. **Generator:** runs one seeded worker per customer, injects edge cases, batch-inserts
   into `events` and writes the ground truth to `test.expected_attribution`.
3. **`events`:** the raw event log. Duplicates are kept here and removed during refreshes.
4. **Refreshable views:** every 5 minutes they build `sessions`, merge them into
   `profiles` (one per person, across every cookie they identified on) and stamp
   `conversions` with all three attribution models.
5. **Verification:** a full outer join of `conversions` against the ground truth that
   flags wrong, missing and invented orders.

## Status

The project is being rebuilt to match the current spec.

| # | Milestone | Status |
| --- | --- | --- |
| 1 | Docker Compose up, `events` table created, generator inserting | Done |
| 2 | Sessions refresh with deduplication and the 30-minute timeout | Done |
| 3 | Profiles stitched by identity, with first touch | Done |
| 4 | Conversions stamped with all three models | Done |
| 5 | Ground-truth table and verification query passing | Done |
| 6 | Load test at 10,000 events/sec, with results in this README | Run: ingest holds 10k, refreshes run out of memory at 10k ([results](#load-test-results)) |

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
row per cookie to `test.customers` (`cookie_id`, `seed`, `start`, `late_sender`); a
customer with a second device or a cleared cookie has one row for each. A customer's seed
and start time rebuild every event they sent. To see what was injected for a cookie:

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

### Profiles

A profile is one person: every cookie that ever carried their `user_id`, so a cleared
cookie or a phone joins the profile once the person checks out on it. A cookie that never
identifies keeps a provisional profile keyed by the cookie (`kind = 'cookie'`). Three
views run in a chain every 5 minutes: `mv_sessions`, then `mv_identity`, then `mv_profiles`.

Read profiles through `profiles_current`, which merges the partial states and hides
provisional profiles whose cookie has since been linked to a person:

```sql
SELECT profile_key, kind, cookies, length(sessions) AS sessions,
       first_touch_channel, first_touch_time, email, name
FROM attribution.profiles_current
WHERE kind = 'user' AND length(cookies) > 1   -- people seen on several cookies
LIMIT 10;

-- Which person a cookie belongs to
SELECT cookie_id, argMinMerge(user_id) AS user_id, min(linked_at) AS linked_at
FROM attribution.identity_links WHERE cookie_id = 'cookie_...' GROUP BY cookie_id;
```

`first_touch_time` is NULL for someone with no landing yet. Profiles link on `user_id` (and
`email`, which always comes with it), never on `name`: names are not unique.

### Conversions

`mv_conversions` runs after `mv_profiles` and stamps every order with three models:

- **First-touch:** the person's first landing ever, across all their cookies.
- **Last-touch:** the latest landing since their previous order.
- **U-shaped:** the lifetime first touch plus the landings since the previous order:
  40% first, 40% last, 20% split across the middle, summed per channel.

When something changes for a person (a late landing, a newly linked cookie, another
order), their recent orders are stamped again. `conversions` keeps every stamp and the
latest wins, so read it through `conversions_current`:

```sql
-- Revenue by channel under each model
SELECT channel,
       sumIf(revenue, first_touch = channel) AS first_touch_revenue,
       sumIf(revenue, last_touch = channel) AS last_touch_revenue,
       sum(revenue * arraySum(arrayMap(t -> if(t.channel = channel, t.credit, 0), u_shaped))) AS u_shaped_revenue
FROM attribution.conversions_current
ARRAY JOIN arrayDistinct(arrayConcat([first_touch, last_touch], u_shaped.channel)) AS channel
WHERE channel != ''
GROUP BY channel ORDER BY u_shaped_revenue DESC;

-- How orders were attributed
SELECT conversion_type, attribution_status, count() FROM attribution.conversions_current
GROUP BY ALL ORDER BY ALL;

-- Every stamp of one order, newest first (re-stamps after late data)
SELECT stamped_at, attribution_status, first_touch, last_touch, u_shaped
FROM attribution.conversions WHERE order_id = 'order_...' ORDER BY stamped_at DESC;
```

### Verify the pipeline

The generator writes the correct answer for every order to `test.expected_attribution`,
computed independently of the SQL. To check the pipeline against it:

```sh
cd generator
python3 -m verify   # exits 1 if the check fails
```

Output from a fresh run, 40 minutes after startup at 20 arrivals/sec:

```
judged     723
matched    723
wrong      0
missing    0
invented   0
gap_device 0
gap_window 0
pass       1
```

Early on, most finished customers are single-visit, so the gaps are 0; they appear as
customers with second devices finish over the following hours.

- **judged:** only customers who are finished (their last delivery is behind the
  pipeline), so an order still waiting for a late landing or a phone login isn't judged
  too early. Single-visit customers finish within minutes; repeat customers take hours.
- **wrong / missing / invented:** must all be 0 to pass. Any mismatch is listed with
  expected and actual values for every field.
- **gap_device / gap_window:** orders where the real person's answer differs from what
  the pipeline can see: a landing on a device they never logged in on, or an input that
  arrived after the order left the 2-day re-stamp window. These are known limitations,
  measured rather than failed.

The same check is in `clickhouse/queries/verify.sql`, and the per-order comparison is the
`test.verification` view:

```sql
SELECT outcome, count() FROM test.verification GROUP BY outcome;
```

## Load test results

Run on 2026-11-13 on a laptop, with Docker limited to 8 CPUs and 7.65 GiB of memory,
shared by ClickHouse and the generator. The generator used `PROCESSES=6`. To reach
10,000 events/sec without hours of warm-up, it ran at 1,500 and then 3,000 arrivals per
second instead of letting repeat visits build up at 1,190. The event volume is real, but
it is mostly first visits.

**Ingest: passes.** The generator reached **10,371 events/sec**. Every insert landed
within 1.6 s of its delivery time (`max lag`), and the generator used about half a CPU core.

**Refreshes: pass up to about 8,600 events/sec, fail at 10,000.** Time and peak memory
for each 5-minute cycle:

| Events/sec | sessions | profiles | Whole chain | Result |
| --- | --- | --- | --- | --- |
| ~140 | 0.1 s / 150 MiB | 0.15 s / 220 MiB | ~0.3 s | ok |
| ~4,900 | 1.4 s / 1.4 GiB | 1.6 s / 2.5 GiB | ~3 s | ok |
| ~8,600 | 2.9 s / 2.5 GiB | 3.9 s / 4.2 GiB | ~7 s | ok |
| ~10,200 | ~4 s / 4.3 GiB | ~4 s / 4.5 GiB | n/a | out of memory |

`identity_links` and `conversions` stayed under 0.2 s and 250 MiB throughout.

- **Time is not the limit.** Even at the failure point the chain used a few seconds of
  its 5-minute budget.
- **Memory is the limit.** The ClickHouse server's cap was 4.65 to 4.95 GiB: by default
  it is a share of the container's memory, which it shares with the generator. At about
  10,200 events/sec, `profiles` failed all 3 retries and `conversions` stopped updating
  because it waits on `profiles`. At the next cycle `sessions` failed as well.
- **The failure does not recover on its own.** After the generator stopped, `sessions`
  caught up on its 4.1 million-event backlog in one run. `profiles` did not: each refresh
  processes everything since its last success in one go, so its input kept growing. Its
  next attempt, covering 15 minutes of sessions, needed more than 6.2 GiB and failed again.
- **Correctness under load.** At about 5,000 events/sec, verification matched 84,582 of
  84,588 orders. The 6 wrong orders belonged to customers cut off when the generator was
  restarted for the test: their remaining events were never sent (see
  [Long runs on a laptop](#long-runs-on-a-laptop)), so they were not pipeline errors. The
  10,000 events/sec run was not verified, because `conversions` had stopped updating.

The per-cycle numbers come from `system.query_log` (refresh queries show up as
`INSERT INTO attribution.<table>`), and the failures from `system.view_refreshes`.

## Known limitations

- **Split sessions:** sessions can be extended but not merged, so a late event that falls
  in the gap between two stored sessions leaves the visit split in two. Attribution is
  unaffected, but session counts may be slightly high. The generator never produces this,
  because its visits are at least 31 minutes apart.
- **Devices that never log in:** a cookie joins its person only once they check out on
  it. Sessions on a device or cleared cookie that never reaches checkout stay in a
  separate provisional profile, so the person's real first touch can be missed. Shared
  devices are not simulated.
- **Re-stamping window:** late landings only update orders in the last two daily
  partitions.
- **Batch, not real time:** numbers lag by up to about 5 minutes.
- **Refresh memory grows with the backlog:** each refresh processes everything since its
  last success in one go. On a 7.65 GiB machine, `profiles` runs out of memory at about
  10,000 events/sec and then can't catch up (see [Load test results](#load-test-results)).

## Out of scope

Sub-second freshness, cross-device identity graphs, subscriptions, Kafka, clusters and
replication. Some of these are planned extensions (see the spec).
