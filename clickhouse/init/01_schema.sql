-- =============================================================================
-- Attribution demo schema
--
-- The ClickHouse image runs every script in /docker-entrypoint-initdb.d once,
-- and only when the data directory is empty. To re-run this file after editing
-- it, wipe the volume with: docker compose down -v
--
-- Data flow:
--
--   generator --batch INSERT--> events
--                                 |-- mv_identity_links --> identity_links
--                                 |-- mv_anon_touches   --> anon_touches
--                                 `-- mv_funnel_minute  --> funnel_minute
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
-- re-delivered event_id is stored as a second row. Queries have to allow for that.
--
-- PARTITION BY physically separates data, here one partition per day. Parts only
-- merge within a partition, and whole partitions can be dropped cheaply. Keep
-- partitions coarse: a single insert touching many partitions creates one part
-- for each of them.
-- -----------------------------------------------------------------------------
CREATE TABLE attribution.events (
  event_id UUID,                          -- Idempotency key (not enforced by the engine)
  event_time DateTime64(3),               -- When it happened, in milliseconds
  received_at DateTime64(3) DEFAULT now64(3), -- Set by the server when the insert omits it
  anonymous_id String,                    -- Browser cookie ID
  user_id String,                         -- Customer ID; empty on anonymous client events
  session_id String,                      -- One per visit
  -- LowCardinality dictionary-encodes a column. It suits columns with few distinct
  -- values, making them smaller on disk and faster to filter and group.
  event_type LowCardinality(String),      -- product_view, add_to_cart, order_placed, page_view
  -- Enum8 stores each value as one byte, while queries still use the names.
  source Enum8('client' = 1, 'server' = 2),
  channel LowCardinality(String),         -- google_ads, facebook, email, organic, direct, affiliate
  -- Optional fields use '' or 0 rather than NULL. Non-Nullable columns are the
  -- ClickHouse default because Nullable needs extra storage and processing.
  utm_campaign String,
  product_id String,
  order_id String,                        -- Only on order_placed
  revenue Decimal(12,2)                   -- Only on order_placed; Decimal avoids float rounding
) ENGINE = MergeTree
PARTITION BY toYYYYMMDD(event_time)
ORDER BY (anonymous_id, event_time);


-- -----------------------------------------------------------------------------
-- identity_links: which customer each anonymous ID belongs to (latest link wins)
--
-- ReplacingMergeTree(linked_at) keeps one row per ORDER BY key during merges: the
-- one with the highest linked_at. Merges run in the background at no guaranteed
-- time, so duplicates can be visible at any moment. Read with FINAL, as in
-- "FROM identity_links FINAL", to get the deduplicated result at query time.
--
-- Trade-off: a shared device has one anonymous_id but two customers, and this
-- table only remembers the most recent one.
-- -----------------------------------------------------------------------------
CREATE TABLE attribution.identity_links (
  anonymous_id String,
  user_id String,
  linked_at DateTime64(3)
) ENGINE = ReplacingMergeTree(linked_at)
ORDER BY anonymous_id;

-- A materialized view in ClickHouse is an insert trigger, not a cached query. For
-- each block inserted into the FROM table, it runs this SELECT over ONLY that block
-- and writes the result to the TO table. It therefore never re-reads events, and it
-- does not backfill rows that were inserted before the view existed.
CREATE MATERIALIZED VIEW attribution.mv_identity_links TO attribution.identity_links AS
SELECT anonymous_id, user_id, event_time AS linked_at
FROM attribution.events
WHERE anonymous_id != '' AND user_id != '';


-- -----------------------------------------------------------------------------
-- anon_touches: first and last marketing touch per anonymous ID, in real time
--
-- Keyed on anonymous_id because client touches have no user_id yet.
--
-- AggregatingMergeTree stores partial aggregation results, called "states". When
-- parts merge, rows with the same ORDER BY key have their states combined.
--
-- AggregateFunction(argMin, String, DateTime64(3)) is the column type for such a
-- state. argMin(channel, event_time) means "the channel on the row with the lowest
-- event_time". The state holds both the best time so far and its channel, which is
-- why a late event with an earlier time can still correct the result.
--
-- The -State and -Merge suffixes are the two halves:
--   argMinState(channel, event_time)  writes a state (in the view below)
--   argMinMerge(first_touch)          combines states into a value (at query time)
--
-- Always use -Merge with GROUP BY when reading, because unmerged parts can hold
-- several rows for one key.
-- -----------------------------------------------------------------------------
CREATE TABLE attribution.anon_touches (
  anonymous_id String,
  first_touch AggregateFunction(argMin, String, DateTime64(3)),
  last_touch  AggregateFunction(argMax, String, DateTime64(3))
) ENGINE = AggregatingMergeTree
ORDER BY anonymous_id;

-- GROUP BY here only groups within one inserted block. Rows for the same anonymous_id
-- in other blocks are combined later, by background merges or by -Merge at query time.
CREATE MATERIALIZED VIEW attribution.mv_anon_touches TO attribution.anon_touches AS
SELECT anonymous_id,
       argMinState(channel, event_time) AS first_touch,
       argMaxState(channel, event_time) AS last_touch
FROM attribution.events
WHERE source = 'client' AND channel != '' AND anonymous_id != ''
GROUP BY anonymous_id;


-- -----------------------------------------------------------------------------
-- funnel_minute: real-time per-minute funnel counts
--
-- The same state pattern, with one row per (minute, channel, event_type):
--   count  how many events
--   uniq   approximate distinct users; its state is a compact hash-based sketch
--          that can be merged without keeping every user_id
--   sum    total revenue
-- -----------------------------------------------------------------------------
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
