-- =============================================================================
-- Verify the pipeline against the generator's ground truth.
--
-- Run with `cd generator && python3 -m verify`, or paste into clickhouse-client.
-- See test.verification (clickhouse/init/05_verification.sql) for how orders are
-- compared and which are judged.
-- =============================================================================

-- 1. Summary. pass = at least one order judged, and none wrong, missing or invented.
--    gap_* count judged orders whose real-person answer differs from what the pipeline
--    can reach, by cause (known limitations, not failures).
SELECT
  countIf(outcome != 'invented') AS judged,
  countIf(outcome = 'matched') AS matched,
  countIf(outcome = 'wrong') AS wrong,
  countIf(outcome = 'missing') AS missing,
  countIf(outcome = 'invented') AS invented,
  countIf(gap_device) AS gap_device,
  countIf(gap_window) AS gap_window,
  judged > 0 AND wrong = 0 AND missing = 0 AND invented = 0 AS pass
FROM test.verification;

-- 2. Every order that isn't matched, with expected vs actual (first 100).
SELECT
  outcome, order_id, seed,
  expected_status, actual_status, expected_type, actual_type,
  expected_first, actual_first, expected_last, actual_last,
  expected_u_shaped, actual_u_shaped, expected_revenue, actual_revenue
FROM test.verification
WHERE outcome != 'matched'
ORDER BY outcome, order_id
LIMIT 100;
