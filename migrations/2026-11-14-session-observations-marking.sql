-- Migration: Amar's alert / entry marks on algo.session_observations
-- ============================================================================
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651), database
--         Algo, schema algo.  CONFIRM THE PROJECT ID BEFORE RUNNING - a second
--         Neon project carries an `algo` schema with the same table names and
--         incompatible columns.
--
-- Agreed with Amar 2026-11-14 ("Marking columns"). He marks the ALERT candle
-- and the ENTRY candle on each day's snapshot chart; these columns hold those
-- marks so they line up directly with the playbooks' own alert register
-- (algo.playbook_alerts, same day):
--   alert_ts     the 5-min candle Amar marks as the alert candle - its START
--                epoch, the same convention as playbook_alerts.alert_ts and
--                candle_5min.candle_ts
--   entry_ts     the 5-min candle Amar marks as the entry candle - its START
--                epoch (as playbook_alerts.entry_ts)
--   entry_price  Amar's entry price
-- An entry cannot come before its alert; either may be NULL (not marked - e.g.
-- a move too quick to have an alert candle).
--
-- Additive: three nullable columns and one check. The 11 existing rows keep
-- NULL in all three (nothing is backfilled - the marks are Amar's, not
-- inferred). No Lambda writes this table; its one reader in code
-- (pattern-recorder/replay/fetch.py) selects * into JSON and is unaffected.
-- A fresh database gets the same from observations/schema.sql.
--
-- APPLIED 2026-11-14 between 22:02 and 22:19 IST to project nameless-mountain-15353651 / database
-- Algo (one transaction, lock_timeout 5s; target checked: database Algo,
-- instrument_master.security_id text): session_observations 13 -> 16 columns,
-- 11 rows untouched (the three new columns NULL on all), check in place.
--
-- ROLLBACK:
--   ALTER TABLE algo.session_observations
--       DROP CONSTRAINT IF EXISTS session_observations_alert_before_entry,
--       DROP COLUMN IF EXISTS alert_ts,
--       DROP COLUMN IF EXISTS entry_ts,
--       DROP COLUMN IF EXISTS entry_price;
-- ============================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE algo.session_observations
    ADD COLUMN IF NOT EXISTS alert_ts     bigint,    -- Amar's alert candle (5-min START epoch)
    ADD COLUMN IF NOT EXISTS entry_ts     bigint,    -- Amar's entry candle (5-min START epoch)
    ADD COLUMN IF NOT EXISTS entry_price  numeric;   -- Amar's entry price

ALTER TABLE algo.session_observations
    ADD CONSTRAINT session_observations_alert_before_entry
        CHECK (alert_ts IS NULL OR entry_ts IS NULL OR entry_ts >= alert_ts);

COMMIT;
