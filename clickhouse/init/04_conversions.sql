-- =============================================================================
-- Conversions (milestone 4)
--
--   profiles --mv_conversions (DEPENDS ON mv_profiles)--> conversions
--
-- Every order is stamped with three attribution models, and re-stamped whenever
-- the person's profile changes (a late landing, a newly linked cookie, another
-- order), so the latest stamp always reflects everything known.
--
-- To load this into an existing volume (after 03_profiles.sql):
--   docker compose exec -T clickhouse sh -c \
--     'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery' \
--     < clickhouse/init/04_conversions.sql
-- =============================================================================


-- -----------------------------------------------------------------------------
-- conversions
--
-- ReplacingMergeTree(stamped_at) keeps one row per ORDER BY key (order_id) during
-- merges: the one with the highest stamped_at. Merges happen at no fixed time, so
-- read with FINAL (or through conversions_current) to see only the latest stamp.
-- An order's partition (day of order_time) never changes, so its stamps always
-- meet in the same partition and can replace each other.
--
-- Attribution rules (agreed for this project):
--   first_touch  The person's first landing ever, up to the order time.
--   last_touch   The latest landing after the previous order, up to this order;
--                empty when there is none.
--   u_shaped     Touches are [lifetime first touch] + the landings since the
--                previous order. 40% first, 40% last, 20% split evenly across the
--                middle; one touch gets 100%, two get 50% each. Credits are summed
--                per channel.
--   status       attributed; unattributed (first order, no landing at all);
--                returning_no_new_touch (repeat order, no landing since the
--                previous one: last_touch empty, 100% to the lifetime first touch).
-- -----------------------------------------------------------------------------
CREATE TABLE attribution.conversions (
  order_id String,
  profile_key String,
  order_time DateTime64(3),
  revenue Decimal(12, 2),
  conversion_type LowCardinality(String),     -- first_order | repeat_order
  attribution_status LowCardinality(String),  -- see above
  first_touch LowCardinality(String),
  last_touch LowCardinality(String),
  u_shaped Array(Tuple(channel String, credit Float64)),
  touches UInt16,                             -- touches in the U-shaped list
  source_received_at DateTime64(3),           -- profiles watermark when stamped
  stamped_at DateTime64(3)
) ENGINE = ReplacingMergeTree(stamped_at)
PARTITION BY toYYYYMMDD(order_time)
ORDER BY order_id;


-- -----------------------------------------------------------------------------
-- mv_conversions
--
--   changed   People whose profile got new rows since the last stamping (minus
--             1 minute of overlap). Linking a cookie always comes with a new
--             session row, so stitching always lands here.
--   people    Those profiles merged: landings and orders, sorted by time.
--   per_order One row per order from the last two days, with the time of the
--             person's previous order (the start of the journey window).
--   touched   The window, the lifetime first touch and the U-shaped touch list.
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW attribution.mv_conversions
REFRESH EVERY 5 MINUTE DEPENDS ON attribution.mv_profiles APPEND TO attribution.conversions AS
WITH
  changed AS (
    SELECT profile_key AS c_key, max(max_received_at) AS c_received
    FROM attribution.profiles
    WHERE kind = 'user'
      AND max_received_at > (SELECT max(source_received_at) FROM attribution.conversions) - INTERVAL 1 MINUTE
    GROUP BY profile_key
  ),
  people AS (
    SELECT profile_key AS p_key,
           arraySort(groupUniqArrayArrayMerge(landings)) AS p_landings,
           arraySort(x -> (x.3, x.1), groupUniqArrayArrayMerge(orders)) AS p_orders
    FROM attribution.profiles
    WHERE kind = 'user' AND profile_key IN (SELECT c_key FROM changed)
    GROUP BY profile_key
  ),
  per_order AS (
    SELECT p_key, p_landings, o, idx,
           if(idx > 1, p_orders[idx - 1].3, toDateTime64('1900-01-01 00:00:00', 3)) AS prev_time
    FROM people
    ARRAY JOIN p_orders AS o, arrayEnumerate(p_orders) AS idx
  ),
  touched AS (
    SELECT *,
           arrayFilter(l -> l.1 <= o.3, p_landings) AS before_order,
           arrayFilter(l -> l.1 > prev_time AND l.1 <= o.3, p_landings) AS window_l,
           length(before_order) > 0 AS has_first,
           before_order[1] AS lifetime_first,
           if(has_first AND lifetime_first.1 <= prev_time,
              arrayConcat([lifetime_first], window_l), window_l) AS touch_list,
           length(touch_list) AS n,
           arrayMap(i -> multiIf(n = 1, 1.0, n = 2, 0.5, i = 1 OR i = n, 0.4, 0.2 / (n - 2)),
                    arrayEnumerate(touch_list)) AS credits,
           arrayReduce('sumMap', [arrayMap(t -> t.2, touch_list)], [credits]) AS by_channel
    FROM per_order
    WHERE o.3 >= toStartOfDay(now()) - INTERVAL 1 DAY
  )
SELECT
  o.1 AS order_id,
  p_key AS profile_key,
  o.3 AS order_time,
  o.2 AS revenue,
  if(idx = 1, 'first_order', 'repeat_order') AS conversion_type,
  multiIf(notEmpty(window_l), 'attributed',
          idx = 1, 'unattributed',
          'returning_no_new_touch') AS attribution_status,
  if(has_first, lifetime_first.2, '') AS first_touch,
  if(notEmpty(window_l), window_l[-1].2, '') AS last_touch,
  CAST(arrayZip(by_channel.1, by_channel.2), 'Array(Tuple(channel String, credit Float64))') AS u_shaped,
  n AS touches,
  (SELECT max(c_received) FROM changed) AS source_received_at,
  now64(3) AS stamped_at
FROM touched;


-- -----------------------------------------------------------------------------
-- conversions_current: the latest stamp of every order
-- -----------------------------------------------------------------------------
CREATE VIEW attribution.conversions_current AS
SELECT * EXCEPT (source_received_at)
FROM attribution.conversions FINAL;
