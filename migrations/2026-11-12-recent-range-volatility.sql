-- Migration: regime's volatility test on the last 30 minutes (option 4)
-- ============================================================================
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651), database
--         Algo, schema algo.  CONFIRM THE PROJECT ID BEFORE RUNNING - a second
--         Neon project carries an `algo` schema with the same table names and
--         incompatible columns.
--
-- Agreed with Amar 2026-11-12 (plan choices A2 / B2 / C1 / D1, window 6 candles):
--   1. algo.intraday_width_profile  - NEW. The normal 5-min candle width at
--      each clock slot over the previous 20 sessions, built once each morning
--      by daily-market-sentiment (09:35) for that day's session.
--   2. algo.intraday_fno_data.recent_range_pct        - written by
--      algo.intraday_fno_data.recent_range_vs_normal    intraday-market-sentiment
--      algo.intraday_fno_data.recent_range_from_ts      on every 15-min run
--
-- WHY. market-classifier's volatility veto compared the day's range since 09:15
-- with the VIX-implied move scaled by sqrt(time elapsed). At 09:45 only 8% of
-- the session has elapsed, so the opening range alone tripped it: the 09:45 row
-- read volatile-expansion on 82% of 34 sessions (2026-09-15 .. 11-12), and on
-- 2026-11-12, an inside day, until 11:30. The replacement judges the last six
-- closed 5-min candles against what is normal for those clock times.
--
-- Everything here is additive. The three new columns are nullable, so existing
-- rows and the running intraday-market-sentiment are unaffected until its new
-- code is deployed: it names its columns explicitly. NULL on a row means
-- "written before this column existed" - or, after the deploy, that the
-- measurement could not be made (the run then raises; see the function README).
--
-- A fresh database gets these from daily-market-sentiment/schema.sql and
-- intraday-market-sentiment/schema.sql instead; this file is only for the
-- deployed tables.
--
-- APPLIED 2026-11-12 21:28 IST to project nameless-mountain-15353651 / database
-- Algo (one transaction, lock_timeout 5s; target checked: database Algo,
-- instrument_master.security_id text): intraday_fno_data 79 -> 82 columns, 936
-- rows untouched (the three new columns NULL on all); intraday_width_profile
-- created empty, 10 columns, PK + FK in place. Rollout step 1 deployed right
-- after: daily-market-sentiment (CodeSha256 BmNaOQJQ..., previous rY2iqUZd...)
-- and intraday-market-sentiment (QkQWsZL/..., previous fy8jiQIJ...).
-- First live day 2026-11-13: the 09:35 build wrote 75 rows (20 sessions
-- 10-14 .. 11-12) and every snapshot carried the ratio, both equal to an
-- independent recompute. Step 2 (layer market-classifier:3 + market-classifier
-- code) went live 2026-11-13 12:31:58 IST.
--
-- ROLLBACK:
--   ALTER TABLE algo.intraday_fno_data DROP COLUMN IF EXISTS recent_range_pct,
--                                      DROP COLUMN IF EXISTS recent_range_vs_normal,
--                                      DROP COLUMN IF EXISTS recent_range_from_ts;
--   DROP TABLE IF EXISTS algo.intraday_width_profile;
-- ============================================================================

BEGIN;

-- 1. The morning normal-width table ---------------------------------------------
--    One row per 5-min slot of ONE session: 75 rows a day (09:15 .. 15:25).
--    slot_ts is that slot's epoch ON THE SESSION THE ROW IS FOR - the same value
--    as that day's candle_5min.candle_ts - so a run joins today's candles to it
--    directly. No clock-of-day value is stored, so no timezone arithmetic.
CREATE TABLE IF NOT EXISTS algo.intraday_width_profile (
    security_id        text     NOT NULL,
    instrument_type    text     NOT NULL,
    slot_ts            bigint   NOT NULL,   -- this slot on trade_date (= that day's candle_ts)
    trade_date         bigint   NOT NULL,   -- IST midnight of the session it is for
    normal_width_pct   numeric  NOT NULL,   -- median of (high - low) / close x 100 at this slot
    samples            integer  NOT NULL,   -- sessions that had a bar at this slot
    sessions           integer  NOT NULL,   -- sessions in the window (20)
    first_session      bigint   NOT NULL,   -- IST midnight of the oldest session used
    last_session       bigint   NOT NULL,   -- IST midnight of the newest (the previous session)
    created_at         bigint   NOT NULL,

    PRIMARY KEY (security_id, instrument_type, slot_ts),
    FOREIGN KEY (security_id, instrument_type)
        REFERENCES algo.instrument_master (security_id, instrument_type)
);

-- 2. The 30-minute width on every snapshot ------------------------------------------
--    recent_range_pct        sum of (high - low) / close x 100 over the last six
--                            CLOSED 5-min candles of today at the run
--    recent_range_vs_normal  that sum / the sum of those six slots' normal_width_pct
--    recent_range_from_ts    candle_ts of the first of the six (the window is
--                            recent_range_from_ts .. + 30 min)
ALTER TABLE algo.intraday_fno_data
    ADD COLUMN IF NOT EXISTS recent_range_pct        numeric,
    ADD COLUMN IF NOT EXISTS recent_range_vs_normal  numeric,
    ADD COLUMN IF NOT EXISTS recent_range_from_ts    bigint;

COMMIT;
