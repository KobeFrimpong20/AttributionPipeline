# ClickHouse Attribution Pipeline — PRD

A single-node ClickHouse pipeline that ingests 10,000 events/sec and computes first-touch, last-touch and U-shaped attribution in 5-minute incremental refreshes, checked against generator ground truth.

## Goals

- Sustain 10,000 events/sec end to end: inserts keep up and every 5-minute refresh finishes before the next, with no growing backlog.
-
- Attribute every order with first-touch, last-touch and U-shaped models.
- Prove correctness under messy data (late events, duplicates, repeat customers, anonymous sessions before checkout, orders with no landings, cleared cookies and second devices) with a single verification query.
- Stitch every cookie a person uses into one profile through deterministic identifiers (`user_id`, `email`).

## Non-goals

- Sub-second freshness. Refreshes run every 5 minutes, on purpose.
- Probabilistic matching, including linking on name. Names are stored on the profile but never used to link: they are not unique.
- Shared devices (two people on one browser).
- Subscriptions (planned extension).
- Kafka, clusters and replication.

## Architecture

1. **Journey model** (state machine) defines customer journeys.
2. **Generator** runs one seeded worker per customer, injects edge cases, batch-inserts into `events`, and writes ground truth into `test.expected_attribution`.
3. **`events`**: raw, append-only MergeTree. No deduplication.
4. **Refreshable views** (every 5 min, APPEND mode, chained with DEPENDS ON) build `sessions`, then `identity_links`, then `profiles`, then stamp `conversions`.
5. **Verification** (`test.verification` view, `clickhouse/queries/verify.sql`, `python -m verify`) compares `conversions` with `test.expected_attribution`.

Server events are enriched upstream with the browser cookie and aliases before ingestion, so every event arrives with a `cookie_id`.

## Event model

Events follow a strict event spec: each event type (product\_viewed, added\_to\_cart, order\_placed, etc.) has its own defined properties. All types land in one table. Fields used by refreshes or attribution get their own columns. Everything else goes in a `properties` column of type Map(String, String). Producers are assumed to send valid, spec-conforming events; validation at ingestion is out of scope.

| Field | Type | Notes |
| --- | --- | --- |
| event\_id | UUID | Idempotency key; dedup happens in refreshes |
| event\_type | LowCardinality(String) | Name from the event spec |
| event\_time | DateTime64(3) | When it happened |
| received\_at | DateTime64(3) | When ingested; drives incremental refresh |
| cookie\_id | String | Browser cookie; copied onto server events upstream |
| user\_id | String | From aliases, when known |
| source | Enum8 | client or server |
| is\_landing | Bool | True on arrival from a channel (new UTM or click ID) |
| channel | LowCardinality(String) | Set on landings |
| order\_id | String | order\_placed only |
| revenue | Decimal(12,2) | order\_placed only |
| properties | Map(String, String) | Type-specific properties: UTM campaign, click ID, product details, email, name, phone, checkout and cart tokens |

## Tables

| Table | Engine | Key | Holds |
| --- | --- | --- | --- |
| events | MergeTree | PARTITION BY day of event\_time; ORDER BY (cookie\_id, received\_at) | Raw events, duplicates included |
| sessions | AggregatingMergeTree | cookie\_id, session\_start (start as first seen; never changes) | Event IDs, first/last event time, landings as (time, channel), orders as (order\_id, revenue, time), user\_id via argMaxIf, max received\_at (the watermark). Every column ignores repeated input |
| identity\_links | AggregatingMergeTree | cookie\_id | The person (`user_id`) a cookie belongs to, from the first session that carried one |
| profiles | AggregatingMergeTree | profile\_key (`user_id`, or the cookie for a never-identified cookie), kind | Cookies, sessions, first touch via argMinIf((time, channel)), landings as (time, channel, cookie), orders, email and name via argMaxIf, max received\_at (the watermark). Read through the `profiles_current` view |
| conversions | ReplacingMergeTree(stamped\_at), PARTITION BY day of order\_time | order\_id | Profile, order time, revenue, conversion\_type (first\_order / repeat\_order), attribution\_status, first touch, last touch, U-shaped as Array of (channel, credit) summed per channel, touch count. Read through `conversions_current` (FINAL) |
| test.customers | MergeTree | cookie\_id | One row per simulated cookie: seed, start, late sender, and when the customer's last delivery arrives |
| test.expected\_attribution | MergeTree | order\_id | Generator ground truth per order: the reachable answer, the real-person answer, and why they differ (gap\_device, gap\_window) |
| test.expected\_attribution | MergeTree | order\_id | Generator ground truth per order |

## Refresh logic (every 5 minutes)

1. Read only the last two `event_time` partitions, then filter to rows with `received_at` after the watermark (the highest `received_at` already in `sessions`) minus a 1-minute overlap. The overlap covers inserts that became visible after the previous run; late events need no overlap, since they get a fresh `received_at`.
2. Deduplicate by construction: every `sessions` column uses an aggregate that ignores repeated input (min, max, sets), so duplicate sends, overlap re-reads and repeated runs all fold away. `LIMIT 1 BY event_id` trims each batch.
3. Sessionise per cookie: a session ends after 30 minutes of inactivity, and each event extends it by 30 minutes. New events are placed on one timeline with the cookie's stored sessions that could still be open, and join the stored session in their group, keeping its key. A late event before a session's start moves `first_event` earlier; the key never changes. Campaign changes stay in the same session.
4. Record a touch only on landings. Every landing in a session counts.
5. Link cookies to people: a cookie belongs to the `user_id` of the first session that carried one (`identity_links`). Then build profiles: new session rows go to the person if their cookie is linked, otherwise to a provisional profile keyed by the cookie. When a cookie is newly linked, all its stored sessions are re-emitted into the person's profile (safe, since every aggregate ignores repeats), and `profiles_current` hides the provisional profile. Email and name come from events. Because `user_id` and `email` identify exactly one person, following chains of links never merges two profiles.
6. Stamp orders: every order from the last two days of people whose profile got new rows since the last stamping (a new order, a late landing, a newly linked cookie) is stamped again, and the latest stamp wins. Linking a cookie always comes with a new session row, so stitching always triggers a re-stamp.

## Attribution rules

- **Journey window:** landings since the customer's previous order, up to the order time.
- **A touch is a landing.** Landings after the order never count for it.
- **First-touch:** the person's first landing ever up to the order (acquisition channel), across every cookie linked to them, including anonymous sessions before checkout.
- **Last-touch:** the latest landing in the journey window. Empty when the window has none.
- **U-shaped:** the touches are the lifetime first touch followed by the landings in the journey window (the first touch is not repeated when it is already in the window). 40% first, 40% last, 20% split evenly across the middle; one touch gets 100%, two get 50% each. Credits are summed per channel.
- **No landings:** a first order with no landing at all is `unattributed`, with every model empty. A repeat order with no landing since the previous order is `returning_no_new_touch`: last-touch is empty and U-shaped gives 100% to the lifetime first touch.

## Mock data

- **Journey model:** a state machine (landing → product view → add to cart → order) with probabilistic transitions.
- **Generator:** one worker per customer, seeded per worker for reproducibility, run as separate processes for throughput.
- **Injected edge cases:** late events, duplicate sends, repeat customers, anonymous sessions before checkout, orders with no landings, cleared cookies (3% of return visits) and second devices (20% of customers, 30% of their return visits). A new cookie is anonymous until the person checks out on it. Shared devices are not simulated.

## Verification

- **Ground truth:** the generator computes every order's answer independently of the SQL (`generator/attribution.py`) and writes it in the same flush as the order's server `order_placed`. It records two answers:
  - **reachable:** landings on cookies the person logged in on, arriving while the order could still be re-stamped. This is what the pipeline should produce, and verification checks against it.
  - **real:** every landing the person made.
- **Known gaps, by cause:** `gap_device` (a landing on a device that never logged in) and `gap_window` (an input that arrived after the re-stamp window). They are reported, not failed.
- **Judged orders:** only customers whose last delivery is at least two minutes behind the conversions watermark, so nothing about them can still change.
- **Comparison:** full outer join of `conversions_current` against the judged expected rows on `order_id`. Status, conversion type, first touch, last touch and touch count must be equal, U-shaped credits equal to 9 decimals per channel, revenue within one cent. Orders the pipeline stamped but the generator never wrote are invented.
- **Pass:** at least one order judged, and zero wrong, missing or invented orders. The pipeline is deterministic, so any mismatch is a bug.

## Performance target

10,000 events/sec, measured end to end: sustained inserts, refreshes finishing inside 5 minutes, and a flat backlog over a long run.

## Known limitations

- **Split sessions:** sessions can be extended but not merged. A late event landing in the gap between two stored sessions leaves the visit split in two. Attribution is unaffected, since touches are counted per landing. Session counts may be slightly inflated.
- **Devices that never log in:** a cookie is joined to its person only once they check out on it. Sessions on a device or cleared cookie that never reaches checkout stay in a separate provisional profile, so the person's real first touch can be missed. Shared devices are not simulated; the first person to identify on a cookie owns it.
- **First touch can change after an order:** stitching a cookie can bring in an earlier landing after the person's orders were stamped. Conversions re-stamp orders in the last two partitions.
- **Re-stamping window:** only orders from the last two days are re-stamped. Older orders keep their last stamp.
- **Batch, not real time:** numbers lag by up to about 5 minutes.

## Future optimisations

- Store recent order times on the profile so stamping doesn't scan `conversions` for the previous order.
- Rebuild affected sessions with a ReplacingMergeTree to fix split sessions.
- Add subscriptions using the `conversion_type` column.
- Put Kafka in front of ingestion for traffic spikes.
- Handle shared devices by letting a cookie's ownership change over time.

## Milestones

1. Docker Compose up, `events` table created, generator inserting.
2. Sessions refresh with deduplication and the 30-minute timeout.
3. Profiles stitched by identity, with first touch.
4. Conversions stamped with all three models.
5. Ground-truth table and verification query passing.
6. Load test at 10,000 events/sec, with results in the README.
