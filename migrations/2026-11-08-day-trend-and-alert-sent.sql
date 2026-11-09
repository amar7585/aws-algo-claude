-- Migration: the classifier's day trend + v2's send-once marks
-- ============================================================================
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651), database
--         Algo, schema algo.  CONFIRM THE PROJECT ID BEFORE RUNNING - a second
--         Neon project carries an `algo` schema with the same table names and
--         incompatible columns.
--
-- Adds (plan v3, agreed with Amar 2026-11-07/08):
--   1. algo.intraday_sentiments.day_trend      - written by market-classifier
--   2. algo.pattern_events.telegram_sent_at    - written by pattern-recorder
--      algo.pattern_events.manager_sent_at
--
-- All three are additive and nullable, so existing rows and the running
-- functions are unaffected: both writers name their columns explicitly, and
-- pattern-recorder's re-derivation upsert sets only the columns in its own list,
-- so it can never clear a sent mark.
--
-- A fresh database gets these from market-classifier/schema.sql and
-- pattern-recorder/schema.sql instead; this file is only for the deployed tables.
--
-- APPLIED 2026-11-09 10:20 IST to project nameless-mountain-15353651 / database
-- Algo (one transaction, lock_timeout 5s): intraday_sentiments 21 -> 22 columns,
-- 867 rows untouched (day_trend NULL on all); pattern_events 35 -> 37 columns,
-- 19 rows untouched (both marks NULL); CHECK constraint in place.
--
-- ROLLBACK:
--   ALTER TABLE algo.intraday_sentiments DROP COLUMN IF EXISTS day_trend;
--   ALTER TABLE algo.pattern_events DROP COLUMN IF EXISTS telegram_sent_at,
--                                   DROP COLUMN IF EXISTS manager_sent_at;
-- ============================================================================

BEGIN;

-- 1. The day trend ------------------------------------------------------------
--    The T1 level rule on the index's 5-min closes from 09:30: a close above
--    BOTH the previous session's high and the 09:15-09:25 range high turns it
--    'up'; a close below both of those turns it off. 'down' is the mirror.
--    'none' is stored explicitly, so NULL means only "written before this
--    column existed" (every row up to the deploy) - never "no trend".
ALTER TABLE algo.intraday_sentiments
    ADD COLUMN IF NOT EXISTS day_trend text;
ALTER TABLE algo.intraday_sentiments
    ADD CONSTRAINT intraday_sentiments_day_trend
    CHECK (day_trend IN ('up', 'down', 'none'));

-- 2. Send-once marks ------------------------------------------------------------
--    Epoch seconds of the send; NULL = not sent. An event that did not pass the
--    alert rule stays NULL in both, as does every v2.0 row.
ALTER TABLE algo.pattern_events
    ADD COLUMN IF NOT EXISTS telegram_sent_at bigint,
    ADD COLUMN IF NOT EXISTS manager_sent_at  bigint;

COMMIT;
