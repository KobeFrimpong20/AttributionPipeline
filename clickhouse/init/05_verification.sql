-- =============================================================================
-- Verification (milestone 5)
--
-- test.verification compares the pipeline's latest stamp of every order
-- (attribution.conversions_current) with the generator's ground truth
-- (test.expected_attribution), one row per order. clickhouse/queries/verify.sql
-- and `python -m verify` summarise it.
--
-- Only finished customers are judged: those whose last delivery is at least two
-- minutes behind the conversions watermark, so nothing about them can still change.
-- An order is "invented" if the pipeline stamped it but the generator never wrote
-- it (only checked for orders older than the watermark, since the truth row is
-- inserted a moment after its order event).
--
-- To load this into an existing volume (after 04_conversions.sql):
--   docker compose exec -T clickhouse sh -c \
--     'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery' \
--     < clickhouse/init/05_verification.sql
-- =============================================================================
CREATE VIEW test.verification AS
WITH
  (SELECT max(source_received_at) FROM attribution.conversions) AS watermark,
  finished AS (
    SELECT seed FROM test.customers
    GROUP BY seed
    HAVING max(last_delivery_at) + INTERVAL 2 MINUTE < watermark
  ),
  expected AS (
    SELECT * FROM test.expected_attribution WHERE seed IN (SELECT seed FROM finished)
  ),
  actual AS (
    SELECT * FROM attribution.conversions_current
    WHERE order_id IN (SELECT order_id FROM expected)
       OR (order_id NOT IN (SELECT order_id FROM test.expected_attribution)
           AND order_time < watermark - INTERVAL 2 MINUTE)
  )
SELECT
  if(e.order_id != '', e.order_id, a.order_id) AS order_id,
  e.seed AS seed,
  multiIf(
    a.order_id = '', 'missing',
    e.order_id = '', 'invented',
    e.status = a.attribution_status
      AND e.conversion_type = a.conversion_type
      AND e.first_touch = a.first_touch
      AND e.last_touch = a.last_touch
      AND e.touches = a.touches
      -- Same channels, and credits equal to 9 decimal places.
      AND arrayMap(x -> x.channel, arraySort(x -> x.channel, e.u_shaped))
        = arrayMap(x -> x.channel, arraySort(x -> x.channel, a.u_shaped))
      AND arrayMap(x -> round(x.credit, 9), arraySort(x -> x.channel, e.u_shaped))
        = arrayMap(x -> round(x.credit, 9), arraySort(x -> x.channel, a.u_shaped))
      AND abs(e.revenue - a.revenue) < 0.01,
    'matched', 'wrong') AS outcome,
  e.gap_device AS gap_device,
  e.gap_window AS gap_window,
  e.status AS expected_status, a.attribution_status AS actual_status,
  e.conversion_type AS expected_type, a.conversion_type AS actual_type,
  e.first_touch AS expected_first, a.first_touch AS actual_first,
  e.last_touch AS expected_last, a.last_touch AS actual_last,
  e.u_shaped AS expected_u_shaped, a.u_shaped AS actual_u_shaped,
  e.revenue AS expected_revenue, a.revenue AS actual_revenue,
  e.real_first_touch AS real_first, e.real_u_shaped AS real_u_shaped
FROM expected AS e
FULL OUTER JOIN actual AS a ON e.order_id = a.order_id;
