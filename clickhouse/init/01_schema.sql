-- =============================================================================
-- Attribution demo schema
--
-- The ClickHouse image runs every script in /docker-entrypoint-initdb.d once,
-- and only when the data directory is empty. To re-run this file after editing
-- it, wipe the volume with: docker compose down -v
--
-- Data flow:
--
--   generator --batch INSERT--> attribution.events     (what production would see)
--             --batch INSERT--> test.customers         (generator metadata only)
--
-- The refreshable views that build sessions, profiles and conversions come in
-- later milestones.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- events: the raw event log and the source of truth
--
-- MergeTree is ClickHouse's core table engine. Each INSERT writes a new immutable
-- "part" to disk, sorted by ORDER BY, and background merges combine small parts
-- into larger ones. Many small inserts create parts faster than merges can
-- combine them, which is why the generator inserts in batches.
--
-- ORDER BY sets the on-disk sort order and the sparse primary index. Unlike a
-- primary key in Postgres or MySQL, it does NOT enforce uniqueness, so a
-- re-delivered event_id is stored as a second row. The refreshes deduplicate.
--
-- ORDER BY (cookie_id, received_at) keeps each customer's events together, in
-- arrival order, which is how the incremental refreshes read them.
--
-- PARTITION BY physically separates data, here one partition per day of
-- event_time. Parts only merge within a partition, and whole partitions can be
-- dropped cheaply. The refreshes only read the last two partitions.
-- -----------------------------------------------------------------------------
CREATE TABLE attribution.events (
  event_id UUID,                          -- Idempotency key (not enforced by the engine)
  -- LowCardinality dictionary-encodes a column. It suits columns with few distinct
  -- values, making them smaller on disk and faster to filter and group.
  event_type LowCardinality(String),      -- product_viewed, order_placed, ...
  event_time DateTime64(3),               -- When it happened, in milliseconds
  received_at DateTime64(3) DEFAULT now64(3), -- When it was inserted; set by the server
  cookie_id String,                       -- Browser cookie; copied onto server events upstream
  user_id String,                         -- '' until the customer identifies at checkout
  -- Enum8 stores each value as one byte, while queries still use the names.
  source Enum8('client' = 1, 'server' = 2),
  is_landing Bool,                        -- Arrival from a channel
  channel LowCardinality(String),         -- Set on landings only
  -- Optional fields use '' or 0 rather than NULL. Non-Nullable columns are the
  -- ClickHouse default because Nullable needs extra storage and processing.
  order_id String,                        -- order_placed only
  revenue Decimal(12,2),                  -- order_placed only; Decimal avoids float rounding
  -- Type-specific properties from the event spec: UTM tags, click IDs, product
  -- details, cart and checkout tokens, email.
  properties Map(String, String)
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(event_time)
ORDER BY (cookie_id, received_at);


-- -----------------------------------------------------------------------------
-- test.customers: one row per simulated cookie
--
-- Generator metadata lives in its own database so the pipeline can never read it.
-- seed and start rebuild a customer's exact events, so any cookie in events can be
-- traced back to its input:  python -m explain --seed <seed> --start <start>
-- start keeps microseconds because the replay needs the exact arrival time. A
-- customer with a second device or a cleared cookie has one row per cookie.
-- last_delivery_at is when the customer's last event arrives; verification only
-- judges customers once that has passed.
-- -----------------------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS test;

CREATE TABLE test.customers (
  cookie_id String,
  seed UInt64,
  start DateTime64(6, 'UTC'),
  late_sender Bool,
  last_delivery_at DateTime64(3, 'UTC')
) ENGINE = MergeTree
ORDER BY cookie_id;


-- -----------------------------------------------------------------------------
-- test.expected_attribution: the correct answer for every order (ground truth)
--
-- Written by the generator in the same flush as the order's server order_placed,
-- computed by generator/attribution.py independently of the SQL. Two answers:
--   status ... touches      reachable: what the pipeline can see (landings on
--                           cookies the person logged in on, arriving while the
--                           order could still be re-stamped). Verification checks
--                           the pipeline against this.
--   real_*                  the real person: every landing they made.
-- gap_device / gap_window say why real differs from reachable: a landing on a
-- device that never logged in, or an input that arrived after the re-stamp window.
-- -----------------------------------------------------------------------------
CREATE TABLE test.expected_attribution (
  order_id String,
  user_id String,
  seed UInt64,
  order_time DateTime64(3, 'UTC'),
  revenue Decimal(12, 2),
  conversion_type LowCardinality(String),
  status LowCardinality(String),
  first_touch LowCardinality(String),
  last_touch LowCardinality(String),
  u_shaped Array(Tuple(channel String, credit Float64)),
  touches UInt16,
  real_status LowCardinality(String),
  real_first_touch LowCardinality(String),
  real_last_touch LowCardinality(String),
  real_u_shaped Array(Tuple(channel String, credit Float64)),
  gap_device Bool,
  gap_window Bool
) ENGINE = MergeTree
ORDER BY order_id;
