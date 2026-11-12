-- =============================================================================
-- Sessions (milestone 2)
--
--   events --every 5 min (refreshable view, APPEND)--> sessions
--
-- A session is a cookie's events with no gap longer than 30 minutes. Each refresh
-- run reads only what arrived since the last run and appends partial sessions;
-- the AggregatingMergeTree combines the parts of a session that arrived in
-- different runs.
--
-- To load this into an existing volume without wiping it:
--   docker compose exec -T clickhouse sh -c \
--     'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery' \
--     < clickhouse/init/02_sessions.sql
-- =============================================================================


-- -----------------------------------------------------------------------------
-- sessions
--
-- Why aggregate states instead of plain rows: one session can be built across
-- several runs (its first events in one run, the rest and a late landing in the
-- next). Rewriting a stored row is slow in ClickHouse; appending a partial state
-- is cheap. Rows with the same ORDER BY key are combined by background merges, so
-- always read with GROUP BY and the -Merge functions (or min/max for the simple
-- columns), e.g.
--
--   SELECT cookie_id, session_start, min(first_event), max(last_event),
--          length(groupUniqArrayMerge(event_ids)) AS events
--   FROM attribution.sessions GROUP BY cookie_id, session_start;
--
-- Every column ignores repeated input (min, max, sets). That is how duplicates are
-- handled: a duplicate send, a row re-read through the overlap, or a whole run
-- repeated, all fold into the same result. Never add count() or sum() here.
--
-- session_start is a stable key: the start time as first seen, never changed. A
-- late event from before it moves first_event earlier instead of creating a new
-- key. Use first_event / last_event for timing, session_start only to group.
--
-- SimpleAggregateFunction stores a plain value and merges it with the named
-- function; it is cheaper than a full AggregateFunction state.
-- -----------------------------------------------------------------------------
CREATE TABLE attribution.sessions (
  cookie_id String,
  session_start DateTime64(3),
  first_event SimpleAggregateFunction(min, DateTime64(3)),
  last_event SimpleAggregateFunction(max, DateTime64(3)),
  event_ids AggregateFunction(groupUniqArray, UUID),
  -- Every landing as (time, channel); U-shaped attribution needs all of them.
  landings AggregateFunction(groupUniqArrayIf, Tuple(DateTime64(3), String), UInt8),
  -- Each order once as (order_id, revenue, time), even when the client and server
  -- both sent it, or it was sent twice. Sum revenue when reading.
  orders AggregateFunction(groupUniqArrayIf, Tuple(String, Decimal(12, 2), DateTime64(3)), UInt8),
  -- The latest non-empty user_id, so anonymous events never overwrite it.
  user_id AggregateFunction(argMaxIf, String, DateTime64(3), UInt8),
  -- Highest received_at processed; the next run's watermark.
  max_received_at SimpleAggregateFunction(max, DateTime64(3))
) ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMMDD(session_start)
ORDER BY (cookie_id, session_start);


-- -----------------------------------------------------------------------------
-- mv_sessions: the refresh
--
-- A refreshable materialized view re-runs its SELECT on a schedule. APPEND TO
-- inserts each run's result into sessions instead of replacing the table, so the
-- query itself must work out what is new:
--
--   batch    Events with received_at after the watermark (the highest received_at
--            already in sessions) minus 1 minute of overlap, from the last two
--            event_time partitions only. The overlap covers inserts stamped before
--            the previous run but visible only after it. Late events need nothing
--            special: they get a fresh received_at when they arrive.
--   stored   This batch's cookies' stored sessions that could still be open.
--   timeline Stored sessions (as spans) and new events (as points) in time order
--            per cookie. A gap of more than 30 minutes after everything before it
--            starts a new group.
--   keyed    Each new event takes the key of the latest stored session in its
--            group that started at or before it, else the group's earliest stored
--            session, else the group's first new event (a brand-new session).
--
-- Known limitation: if a late event bridges two stored sessions, they stay two
-- sessions (stored keys are never merged). The generator never produces this,
-- because its visits are at least 31 minutes apart.
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW attribution.mv_sessions
REFRESH EVERY 5 MINUTE APPEND TO attribution.sessions AS
WITH
  batch AS (
    SELECT event_id AS e_id, event_time AS e_time, received_at AS e_received, cookie_id AS e_cookie,
           user_id AS e_user, is_landing AS e_landing, channel AS e_channel,
           order_id AS e_order, revenue AS e_revenue
    FROM attribution.events
    WHERE event_time >= toStartOfDay(now()) - INTERVAL 1 DAY
      AND received_at > (SELECT max(max_received_at) FROM attribution.sessions) - INTERVAL 1 MINUTE
    LIMIT 1 BY event_id
  ),
  stored AS (
    SELECT cookie_id, session_start, min(first_event) AS s_first, max(last_event) AS s_last
    FROM attribution.sessions
    WHERE cookie_id IN (SELECT e_cookie FROM batch)
      AND session_start >= toStartOfDay(now()) - INTERVAL 2 DAY
    GROUP BY cookie_id, session_start
    HAVING s_last >= (SELECT min(e_time) FROM batch) - INTERVAL 30 MINUTE
  ),
  timeline AS (
    SELECT e_cookie AS t_cookie, e_time AS t_from, e_time AS t_to, 0 AS t_stored,
           toDateTime64(0, 3) AS t_key,
           e_id, e_time, e_received, e_user, e_landing, e_channel, e_order, e_revenue
    FROM batch
    UNION ALL
    SELECT cookie_id, s_first, s_last, 1, session_start,
           toUUID('00000000-0000-0000-0000-000000000000'), s_first, s_first, '', false, '', '', toDecimal64(0, 2)
    FROM stored
  ),
  grouped AS (
    SELECT *,
           sum(t_new) OVER (PARTITION BY t_cookie ORDER BY t_from, t_stored DESC, e_id
                            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS t_group
    FROM (
      SELECT *,
             t_from > max(t_to) OVER (PARTITION BY t_cookie ORDER BY t_from, t_stored DESC, e_id
                                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
                      + INTERVAL 30 MINUTE AS t_new
      FROM timeline
    )
  ),
  keyed AS (
    SELECT *,
           multiIf(
             stored_before > 0, latest_stored_before,
             stored_in_group > 0, earliest_stored,
             first_in_group) AS key
    FROM (
      SELECT *,
             countIf(t_stored = 1) OVER w_before AS stored_before,
             maxIf(t_key, t_stored = 1) OVER w_before AS latest_stored_before,
             countIf(t_stored = 1) OVER w_group AS stored_in_group,
             minIf(t_key, t_stored = 1) OVER w_group AS earliest_stored,
             min(t_from) OVER w_group AS first_in_group
      FROM grouped
      WINDOW
        w_before AS (PARTITION BY t_cookie, t_group ORDER BY t_from, t_stored DESC, e_id
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
        w_group AS (PARTITION BY t_cookie, t_group)
    )
  )
SELECT
  t_cookie AS cookie_id,
  key AS session_start,
  min(e_time) AS first_event,
  max(e_time) AS last_event,
  groupUniqArrayState(e_id) AS event_ids,
  groupUniqArrayIfState((e_time, e_channel), e_landing) AS landings,
  groupUniqArrayIfState((e_order, e_revenue, e_time), e_order != '') AS orders,
  argMaxIfState(e_user, e_time, e_user != '') AS user_id,
  max(e_received) AS max_received_at
FROM keyed
WHERE t_stored = 0
GROUP BY cookie_id, session_start;
