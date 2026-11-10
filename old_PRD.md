# ClickHouse Attribution Demo — Design Spec

A lightweight, production-minded mini pipeline for learning ClickHouse real-time aggregation through marketing attribution.

## Goals

- Learn core ClickHouse concepts: MergeTree engines, materialised views, aggregate function states (`-State` / `-Merge`), and batch inserts.
- Simulate e-commerce user events and compute **first-touch**, **last-touch**, and **U-shaped** attribution.
- Keep it lightweight: one `docker compose up` and a Python generator.

## Non-goals (for now)

- Kafka (phase 2)
- Dashboards or a front end
- Multi-node / replicated ClickHouse

## Architecture

**Phase 1:** Python generator → batch `INSERT` → `events` (MergeTree) → materialised views → aggregate tables

**Phase 2:** Python producer → Kafka → Kafka engine table → materialised view → `events` (everything after that stays the same)

## Repo layout

```
clickhouse-demo/
  docker-compose.yml
  clickhouse/
    init/01_schema.sql
  generator/
    Dockerfile
    requirements.txt
    generate.py
  queries/
    attribution.sql
  README.md
```

## Docker Compose

```yaml
services:
  clickhouse:
    image: clickhouse/clickhouse-server:latest
    ports:
      - "8123:8123"
      - "9000:9000"
    environment:
      CLICKHOUSE_DB: attribution
      CLICKHOUSE_USER: demo
      CLICKHOUSE_PASSWORD: demo
      CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT: 1
    volumes:
      - ./clickhouse/init:/docker-entrypoint-initdb.d
      - ch_data:/var/lib/clickhouse
    ulimits:
      nofile: { soft: 262144, hard: 262144 }
    healthcheck:
      test: ["CMD", "clickhouse-client", "--query", "SELECT 1"]
      interval: 5s
      retries: 20

  generator:
    build: ./generator
    depends_on:
      clickhouse:
        condition: service_healthy
    environment:
      CH_HOST: clickhouse
      CH_USER: demo
      CH_PASSWORD: demo
      CH_DB: attribution
      EVENTS_PER_SEC: 200
      BATCH_SIZE: 10000

volumes:
  ch_data:
```

## Event model

A single `events` table holds both client-side and server-side events.

| Field | Type | Notes |
| --- | --- | --- |
| event\_id | UUID | Idempotency key |
| event\_time | DateTime64(3) | When the event happened (client or server clock) |
| received\_at | DateTime64(3) | When the pipeline ingested it; used to measure lateness and freshness |
| anonymous\_id | String | Browser cookie ID; on every client event, and passed through checkout to server events |
| user\_id | String | Customer ID; empty on anonymous client events, set on server events |
| session\_id | String | One per visit |
| event\_type | LowCardinality(String) | product\_view, add\_to\_cart, order\_placed (page\_view optional) |
| source | Enum8 | client or server |
| channel | LowCardinality(String) | google\_ads, facebook, email, organic, direct, affiliate |
| utm\_campaign | String | Optional |
| product\_id | String | Optional |
| order\_id | String | Only on order\_placed |
| revenue | Decimal(12,2) | Only on order\_placed |

**Identity:** an `identity_links` table maps `anonymous_id` to `user_id`. A link is written the first time an event carries both, usually at checkout. Attribution resolves identity at query time through this table, so links that arrive late still produce correct results.

**Touchpoints** = session-starting events that have a channel. **Conversions** = `order_placed`.

## Schema (01\_schema.sql)

```sql
CREATE TABLE attribution.events (
  event_id UUID,
  event_time DateTime64(3),
  received_at DateTime64(3) DEFAULT now64(3),
  anonymous_id String,
  user_id String,
  session_id String,
  event_type LowCardinality(String),
  source Enum8('client' = 1, 'server' = 2),
  channel LowCardinality(String),
  utm_campaign String,
  product_id String,
  order_id String,
  revenue Decimal(12,2)
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(event_time)
ORDER BY (anonymous_id, event_time);

-- Identity stitching: anonymous_id -> user_id, latest link wins
CREATE TABLE attribution.identity_links (
  anonymous_id String,
  user_id String,
  linked_at DateTime64(3)
) ENGINE = ReplacingMergeTree(linked_at)
ORDER BY anonymous_id;

CREATE MATERIALIZED VIEW attribution.mv_identity_links TO attribution.identity_links AS
SELECT anonymous_id, user_id, event_time AS linked_at
FROM attribution.events
WHERE anonymous_id != '' AND user_id != '';

-- Per-anonymous-ID first and last touch, maintained in real time.
-- Keyed on anonymous_id because client touches have no user_id yet.
CREATE TABLE attribution.anon_touches (
  anonymous_id String,
  first_touch AggregateFunction(argMin, String, DateTime64(3)),
  last_touch  AggregateFunction(argMax, String, DateTime64(3))
) ENGINE = AggregatingMergeTree
ORDER BY anonymous_id;

CREATE MATERIALIZED VIEW attribution.mv_anon_touches TO attribution.anon_touches AS
SELECT anonymous_id,
       argMinState(channel, event_time) AS first_touch,
       argMaxState(channel, event_time) AS last_touch
FROM attribution.events
WHERE source = 'client' AND channel != '' AND anonymous_id != ''
GROUP BY anonymous_id;

-- Real-time per-minute funnel counts
CREATE TABLE attribution.funnel_minute (
  minute DateTime,
  channel LowCardinality(String),
  event_type LowCardinality(String),
  events AggregateFunction(count),
  users AggregateFunction(uniq, String),
  revenue AggregateFunction(sum, Decimal(12,2))
) ENGINE = AggregatingMergeTree
ORDER BY (minute, channel, event_type);

CREATE MATERIALIZED VIEW attribution.mv_funnel_minute TO attribution.funnel_minute AS
SELECT toStartOfMinute(event_time) AS minute, channel, event_type,
       countState() AS events,
       uniqState(user_id) AS users,
       sumState(revenue) AS revenue
FROM attribution.events
GROUP BY minute, channel, event_type;
```

## Attribution models

- **First-touch:** 100% of order revenue goes to the customer's first channel. Join `anon_touches` to `identity_links FINAL`, then `argMinMerge` grouped by `user_id`. Merging states across all of a customer's anonymous IDs is what makes this work.
- **Last-touch:** 100% goes to the last channel before the order. Same join, with `argMaxMerge` (or a lookback at query time for strictness).
- **U-shaped:** 40% first, 40% last, 20% split evenly across middle touches. With 1 touch it gets 100%, and with 2 touches each gets 50%. Each order is its own journey: only touches since the customer's previous order count. For repeat orders, the customer's acquisition channel (first-ever touch) is put at the front of the path, so it keeps first-position credit on repeat revenue. If a repeat order has no new touches, the acquisition channel gets 100%.

First/last-touch query sketch:

```sql
SELECT l.user_id,
       argMinMerge(t.first_touch) AS first_channel,
       argMaxMerge(t.last_touch)  AS last_channel
FROM attribution.anon_touches t
INNER JOIN (SELECT anonymous_id, user_id FROM attribution.identity_links FINAL) l
  USING anonymous_id
GROUP BY l.user_id;
```

U-shaped query sketch:

```sql
WITH links AS (
  SELECT anonymous_id, user_id FROM attribution.identity_links FINAL
),
touches AS (
  SELECT anonymous_id, event_time, channel
  FROM attribution.events
  WHERE source = 'client' AND channel != '' AND anonymous_id != ''
),
-- Acquisition channel = customer's first-ever touch
acquisition AS (
  SELECT l.user_id, argMinMerge(t.first_touch) AS acq_channel
  FROM attribution.anon_touches t
  INNER JOIN links l USING anonymous_id
  GROUP BY l.user_id
),
-- Each order plus the time of the same customer's previous order
orders AS (
  SELECT order_id, user_id, revenue, event_time AS order_time,
         lagInFrame(event_time, 1, toDateTime64(0, 3)) OVER (
           PARTITION BY user_id ORDER BY event_time
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS prev_order_time
  FROM attribution.events
  WHERE event_type = 'order_placed' AND user_id != ''
),
-- LEFT JOINs keep orders with no tracked touches (missing values default to '')
journeys AS (
  SELECT o.order_id, o.revenue,
         -- touches since the previous order, in time order
         arrayMap(x -> x.2, arraySort(x -> x.1,
           groupArrayIf((t.event_time, t.channel),
             t.channel != ''
             AND t.event_time > o.prev_order_time
             AND t.event_time <= o.order_time))) AS recent,
         -- repeat orders: acquisition channel goes first
         arrayFilter(c -> c != '',
           if(o.prev_order_time = toDateTime64(0, 3), recent,
              arrayConcat([any(a.acq_channel)], recent))) AS p,
         -- nothing tracked at all: credit goes to 'unattributed'
         if(empty(p), ['unattributed'], p) AS path
  FROM orders o
  LEFT JOIN links l ON l.user_id = o.user_id
  LEFT JOIN touches t ON t.anonymous_id = l.anonymous_id
  LEFT JOIN acquisition a ON a.user_id = o.user_id
  GROUP BY o.order_id, o.revenue, o.prev_order_time
)
SELECT channel, sum(credit) AS attributed_revenue
FROM (
  SELECT revenue, length(path) AS n,
         arrayJoin(arrayEnumerate(path)) AS i,
         path[i] AS channel,
         revenue * multiIf(n = 1, 1.0, n = 2, 0.5,
                           i = 1 OR i = n, 0.4, 0.2 / (n - 2)) AS credit
  FROM journeys
)
GROUP BY channel ORDER BY attributed_revenue DESC;
```

## Generator (generate.py)

- Uses `clickhouse-connect`.
- **Identity:** each simulated device gets an `anonymous_id`. Client events carry only `anonymous_id`. Server events carry both `anonymous_id` (passed through checkout) and `user_id`.
- **Journeys:** N customers with a weighted channel mix, 1 to 5 sessions each. Every session starts with a client event that has a channel (`direct` counts). Funnel: product\_view → add\_to\_cart (25%) → order\_placed (35% of carts). page\_view is optional browsing noise.
- **Event sources:** client = product\_view (and optional page\_view). Server = add\_to\_cart and order\_placed.
- **Edge cases to exercise the attribution rules:**
  - Repeat customers (about 20% place 2 or more orders), to test separate journeys and acquisition-channel credit.
  - Multi-device customers (one `user_id` with 2 or more `anonymous_id`s), to test stitching.
  - Shared devices (one `anonymous_id` linked to 2 `user_id`s over time), to show the latest-link-wins trade-off.
  - A small share of orders with no tracked client touches, to populate `unattributed`.
  - Late and duplicate events (same `event_id`), to show real-world messiness.
- **Batching:** client-side batches of 10,000 to 50,000 rows (`BATCH_SIZE`), at a steady `EVENTS_PER_SEC`. Aim for about one insert per second per table.

## Production-grade touches (lightweight)

- Batch inserts only. No row-by-row inserts.
- Idempotency via `event_id`, with an optional `ReplacingMergeTree` version as a stretch goal.
- Health checks and init scripts so `docker compose up` works from scratch.
- README with sample queries and a short explanation of `-State` vs `-Merge`.

## Performance targets

The goal is reproducible numbers on a single laptop node, published in the README using a benchmark script.

| Area | Target | How it's measured |
| --- | --- | --- |
| Sustained ingest | 100k+ events/sec for 10 min, no backlog growth | Generator load mode, rows/sec from `system.query_log` |
| Data volume | 100M+ events stored | `system.parts` row count and compressed size |
| Freshness | Event to aggregate under 2 s (p95) | Timestamp marker events, then query the aggregate table |
| First/last-touch query | Under 100 ms over 100M events | `clickhouse-benchmark`, p50/p95 |
| U-shaped query (1 day of orders) | Under 1 s | `clickhouse-benchmark`, p50/p95 |
| Correctness | Duplicates and late events don't change totals | Replay a batch twice and compare the attributed revenue |
| Recovery | Restart with zero data loss | Kill ClickHouse mid-load, then reconcile counts |

Stretch goals that match real attribution problems:

- **Identity stitching:** merge anonymous client IDs with logged-in user IDs, and re-attribute after the merge.
- **Lookback windows:** attribute only to touches within a configurable window, such as 30 days.
- **Backfill:** recompute attribution for a past date range without stopping ingestion.
- **Observability:** insert latency, parts count and merge pressure, all shown from system tables.
- **Write-up:** a short README section on design trade-offs and what changes at 10x and 100x scale.

## Milestones

1. Compose up, schema loads, geeatnerator inserts events.
2. Funnel MV plus per-minute query working.
3. First-touch and last-touch from `user_touches`.
4. U-shaped query matches a hand-checked small sample.
5. **Phase 2:** add Kafka plus a Kafka engine table in front of `events`.

## Prompt for Claude Code

> Build the project described in this spec. Start with milestone 1, run `docker compose up` to verify, then proceed milestone by milestone. Keep code clean and well commented, explaining the ClickHouse concepts as you go, as this is my first ClickHouse project.
