-- =============================================================================
-- Identity and profiles (milestone 3)
--
--   sessions --mv_identity--> identity_links      (cookie -> user_id)
--   sessions + identity_links + events --mv_profiles--> profiles
--
-- A profile is one person: every cookie that ever carried their user_id, across
-- cleared cookies and devices. Links are deterministic (user_id; email always
-- arrives with it). name is stored but never used to link: names are not unique.
-- A cookie that never identifies keeps a provisional profile keyed by the cookie.
--
-- The views run in a chain every 5 minutes: mv_sessions, then mv_identity, then
-- mv_profiles (DEPENDS ON), so each reads its input after the step before has
-- finished. Every aggregate ignores repeated input, as in sessions.
--
-- To load this into an existing volume (after 02_sessions.sql):
--   docker compose exec -T clickhouse sh -c \
--     'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery' \
--     < clickhouse/init/03_profiles.sql
-- =============================================================================


-- -----------------------------------------------------------------------------
-- identity_links: which person each cookie belongs to
--
-- A cookie is linked the first time one of its sessions carries a user_id. Shared
-- devices are not simulated, so a cookie belongs to at most one person; argMin
-- keeps the first user_id seen, which also makes the link stable.
-- -----------------------------------------------------------------------------
CREATE TABLE attribution.identity_links (
  cookie_id String,
  user_id AggregateFunction(argMin, String, DateTime64(3)),
  linked_at SimpleAggregateFunction(min, DateTime64(3)),
  max_received_at SimpleAggregateFunction(max, DateTime64(3))
) ENGINE = AggregatingMergeTree
ORDER BY cookie_id;

CREATE MATERIALIZED VIEW attribution.mv_identity
REFRESH EVERY 5 MINUTE DEPENDS ON attribution.mv_sessions APPEND TO attribution.identity_links AS
SELECT
  cookie_id,
  argMinState(uid, first_event) AS user_id,
  min(first_event) AS linked_at,
  max(row_received) AS max_received_at
FROM (
  -- New partial session rows since this table's watermark (minus 1 minute overlap).
  SELECT cookie_id, finalizeAggregation(user_id) AS uid, first_event, max_received_at AS row_received
  FROM attribution.sessions
  WHERE session_start >= toStartOfDay(now()) - INTERVAL 2 DAY
    AND max_received_at > (SELECT max(max_received_at) FROM attribution.identity_links) - INTERVAL 1 MINUTE
)
WHERE uid != ''
GROUP BY cookie_id;


-- -----------------------------------------------------------------------------
-- profiles
--
-- profile_key is the user_id (kind 'user'), or the cookie for a cookie that has
-- never identified (kind 'cookie', provisional). When a cookie gets linked, all of
-- its stored sessions are re-emitted into the user's profile; the provisional
-- profile stays behind, and profiles_current hides it.
--
-- first_touch is the person's earliest landing ever, across every cookie, as
-- (time, channel). Comparing (time, channel) tuples breaks ties on equal times
-- deterministically. Read it with argMinIfMerge, or use profiles_current.
-- -----------------------------------------------------------------------------
CREATE TABLE attribution.profiles (
  profile_key String,
  kind LowCardinality(String),
  cookies AggregateFunction(groupUniqArrayArray, Array(String)),
  sessions AggregateFunction(groupUniqArrayArray, Array(Tuple(String, DateTime64(3)))),
  first_touch AggregateFunction(argMinIf, Tuple(DateTime64(3), String), Tuple(DateTime64(3), String), UInt8),
  landings AggregateFunction(groupUniqArrayArray, Array(Tuple(DateTime64(3), String, String))),
  orders AggregateFunction(groupUniqArrayArray, Array(Tuple(String, Decimal(12, 2), DateTime64(3)))),
  email AggregateFunction(argMaxIf, String, DateTime64(3), UInt8),
  name AggregateFunction(argMaxIf, String, DateTime64(3), UInt8),
  max_received_at SimpleAggregateFunction(max, DateTime64(3))
) ENGINE = AggregatingMergeTree
ORDER BY (profile_key, kind);


-- -----------------------------------------------------------------------------
-- mv_profiles: three inputs, one row shape, then one GROUP BY
--
--   new_sessions  Partial session rows since the watermark, keyed by the cookie's
--                 user_id if it is linked, else by the cookie.
--   reemit        Every stored session row of cookies linked since the watermark,
--                 keyed by the user_id (moves anonymous history into the person).
--   identity      Events carrying email or name since the watermark, keyed by
--                 their user_id (checkout and order events always carry one).
--
-- One watermark covers all three: the highest received_at already in profiles.
-- Events are read only up to the sessions watermark, so both inputs move together.
-- -----------------------------------------------------------------------------
CREATE MATERIALIZED VIEW attribution.mv_profiles
REFRESH EVERY 5 MINUTE DEPENDS ON attribution.mv_identity APPEND TO attribution.profiles AS
WITH
  links AS (
    SELECT cookie_id AS l_cookie, argMinMerge(user_id) AS l_user, max(max_received_at) AS l_received
    FROM attribution.identity_links
    GROUP BY cookie_id
  ),
  new_links AS (
    SELECT l_cookie, l_user FROM links
    WHERE l_received > (SELECT max(max_received_at) FROM attribution.profiles) - INTERVAL 1 MINUTE
  ),
  session_rows AS (
    SELECT cookie_id AS s_cookie, session_start AS s_start,
           finalizeAggregation(landings) AS s_landings,
           finalizeAggregation(orders) AS s_orders,
           max_received_at AS s_received
    FROM attribution.sessions
  ),
  inputs AS (
    -- new_sessions
    SELECT if(l_user != '', l_user, s_cookie) AS i_key, if(l_user != '', 'user', 'cookie') AS i_kind,
           s_cookie AS i_cookie, s_start AS i_start, s_landings AS i_landings, s_orders AS i_orders,
           '' AS i_email, '' AS i_name, toDateTime64(0, 3) AS i_time, s_received AS i_received
    FROM session_rows LEFT JOIN links ON l_cookie = s_cookie
    WHERE s_start >= toStartOfDay(now()) - INTERVAL 2 DAY
      AND s_received > (SELECT max(max_received_at) FROM attribution.profiles) - INTERVAL 1 MINUTE
    UNION ALL
    -- reemit
    SELECT l_user, 'user', s_cookie, s_start, s_landings, s_orders, '', '', toDateTime64(0, 3), s_received
    FROM session_rows INNER JOIN new_links ON l_cookie = s_cookie
    UNION ALL
    -- identity
    SELECT user_id, 'user', '', toDateTime64(0, 3), [], [],
           properties['email'], properties['name'], event_time, received_at
    FROM attribution.events
    WHERE event_time >= toStartOfDay(now()) - INTERVAL 1 DAY
      AND received_at > (SELECT max(max_received_at) FROM attribution.profiles) - INTERVAL 1 MINUTE
      AND received_at <= (SELECT max(max_received_at) FROM attribution.sessions)
      AND user_id != '' AND (properties['email'] != '' OR properties['name'] != '')
  )
SELECT
  i_key AS profile_key,
  i_kind AS kind,
  groupUniqArrayArrayState(if(i_cookie != '', [i_cookie], [])) AS cookies,
  groupUniqArrayArrayState(if(i_cookie != '', [(i_cookie, i_start)], [])) AS sessions,
  argMinIfState(arrayMin(i_landings), arrayMin(i_landings), length(i_landings) > 0) AS first_touch,
  groupUniqArrayArrayState(arrayMap(l -> (l.1, l.2, i_cookie), i_landings)) AS landings,
  groupUniqArrayArrayState(i_orders) AS orders,
  argMaxIfState(i_email, i_time, i_email != '') AS email,
  argMaxIfState(i_name, i_time, i_name != '') AS name,
  max(i_received) AS max_received_at
FROM inputs
GROUP BY profile_key, kind;


-- -----------------------------------------------------------------------------
-- profiles_current: the way to read profiles
--
-- Merges the partial states and hides provisional cookie profiles whose cookie has
-- since been linked to a person (their sessions now live in the user's profile).
-- first_touch_time is NULL for a person with no landing yet.
-- -----------------------------------------------------------------------------
CREATE VIEW attribution.profiles_current AS
SELECT
  profile_key,
  kind,
  groupUniqArrayArrayMerge(cookies) AS cookies,
  groupUniqArrayArrayMerge(sessions) AS sessions,
  argMinIfMerge(first_touch) AS ft,
  if(ft.2 = '', NULL, ft.1) AS first_touch_time,
  ft.2 AS first_touch_channel,
  groupUniqArrayArrayMerge(landings) AS landings,
  groupUniqArrayArrayMerge(orders) AS orders,
  argMaxIfMerge(email) AS email,
  argMaxIfMerge(name) AS name
FROM attribution.profiles
GROUP BY profile_key, kind
HAVING kind = 'user' OR profile_key NOT IN (SELECT cookie_id FROM attribution.identity_links);
