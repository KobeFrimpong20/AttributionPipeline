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
5. **Verification query** compares `conversions` with `test.expected_attribution`.

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
| conversions | ReplacingMergeTree(stamped\_at) | order\_id | Order time, revenue, conversion\_type, first touch, last touch, U-shaped as Array of (channel, credit) |
| test.expected\_attribution | MergeTree | order\_id | Generator ground truth per order |

## Refresh logic (every 5 minutes)

1. Read only the last two `event_time` partitions, then filter to rows with `received_at` after the watermark (the highest `received_at` already in `sessions`) minus a 1-minute overlap. The overlap covers inserts that became visible after the previous run; late events need no overlap, since they get a fresh `received_at`.
2. Deduplicate by construction: every `sessions` column uses an aggregate that ignores repeated input (min, max, sets), so duplicate sends, overlap re-reads and repeated runs all fold away. `LIMIT 1 BY event_id` trims each batch.
3. Sessionise per cookie: a session ends after 30 minutes of inactivity, and each event extends it by 30 minutes. New events are placed on one timeline with the cookie's stored sessions that could still be open, and join the stored session in their group, keeping its key. A late event before a session's start moves `first_event` earlier; the key never changes. Campaign changes stay in the same session.
4. Record a touch only on landings. Every landing in a session counts.
5. Link cookies to people: a cookie belongs to the `user_id` of the first session that carried one (`identity_links`). Then build profiles: new session rows go to the person if their cookie is linked, otherwise to a provisional profile keyed by the cookie. When a cookie is newly linked, all its stored sessions are re-emitted into the person's profile (safe, since every aggregate ignores repeats), and `profiles_current` hides the provisional profile. Email and name come from events. Because `user_id` and `email` identify exactly one person, following chains of links never merges two profiles.
6. Stamp new orders, and re-stamp orders in the last two partitions so late landings are reflected.

## Attribution rules

- **Journey window:** landings since the customer's previous order, up to the order time.
- **First-touch:** the person's first landing ever (acquisition channel), across every cookie linked to them, including anonymous sessions before checkout, read from the profile state.
- **Last-touch:** the latest landing in the current journey before the order.
- **U-shaped:** 40% first, 40% last, 20% split across the middle. One touch gets 100%, two touches get 50% each. Uses the current journey only.
- **No landings:** a first order is `unattributed`. A repeat order is `returning, no new touch`.

## Mock data

- **Journey model:** a state machine (landing → product view → add to cart → order) with probabilistic transitions.
- **Generator:** one worker per customer, seeded per worker for reproducibility, run as separate processes for throughput.
- **Injected edge cases:** late events, duplicate sends, repeat customers, anonymous sessions before checkout, orders with no landings, cleared cookies (3% of return visits) and second devices (20% of customers, 30% of their return visits). A new cookie is anonymous until the person checks out on it. Shared devices are not simulated.

## Verification

- Full outer join of `conversions FINAL` against `test.expected_attribution` on `order_id`.
- Flags wrong, missing and invented orders. Revenue compared within one cent.
- Run only after the final refresh, including the overlap window, has caught up.
- Every mismatch is either a bug or one of the known limitations below.

## Performance target

10,000 events/sec, measured end to end: sustained inserts, refreshes finishing inside 5 minutes, and a flat backlog over a long run.

## Known limitations

- **Split sessions:** sessions can be extended but not merged. A late event landing in the gap between two stored sessions leaves the visit split in two. Attribution is unaffected, since touches are counted per landing. Session counts may be slightly inflated.
- **Devices that never log in:** a cookie is joined to its person only once they check out on it. Sessions on a device or cleared cookie that never reaches checkout stay in a separate provisional profile, so the person's real first touch can be missed. Shared devices are not simulated; the first person to identify on a cookie owns it.
- **First touch can change after an order:** stitching a cookie can bring in an earlier landing after the person's orders were stamped. Conversions re-stamp orders in the last two partitions.
- **Re-stamping window:** late landings only update orders in the last two partitions. Older orders keep their original stamp.
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
