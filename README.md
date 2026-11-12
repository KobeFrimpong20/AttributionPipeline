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
| 1 | Docker Compose up, `events` table created, generator inserting | Partly done (being reworked to the new event model) |
| 2 | Sessions refresh with deduplication and the 30-minute timeout | Not started |
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

The generator runs behind a Compose profile. Once the generator code is in place, start
it with:

```sh
docker compose --profile generator up --build
```

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

Queries for sessions, profiles, conversions and the verification check will be added
here as each milestone lands.

## Known limitations

- **Split sessions:** sessions can be extended but not merged, so a late event that falls
  in the gap between two stored sessions leaves the visit split in two. Attribution is
  unaffected, but session counts may be slightly high.
- **One cookie is one customer:** profiles are keyed by cookie, with no identity
  resolution. Journeys across devices are not joined, which biases attribution towards
  late-funnel channels. Shared devices are not simulated.
- **Re-stamping window:** late landings only update orders in the last two daily
  partitions.
- **Batch, not real time:** numbers lag by up to about 5 minutes.

## Out of scope

Sub-second freshness, cross-device identity graphs, subscriptions, Kafka, clusters and
replication. Some of these are planned extensions (see the spec).
