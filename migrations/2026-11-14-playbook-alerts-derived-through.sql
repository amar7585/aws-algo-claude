-- Migration: algo.playbook_alerts.derived_through_ts (out-of-order ticks)
-- ============================================================================
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651), database
--         Algo, schema algo.  CONFIRM THE PROJECT ID BEFORE RUNNING - a second
--         Neon project carries an `algo` schema with the same table names and
--         incompatible columns.
--
-- Agreed with Amar 2026-11-14 ("Fix it (option A)"). The 22:58 smoke replayed
-- 2026-11-13's 09:25 tick AFTER that session's full-day register was stored,
-- and the register's "never shrink" check raised: it cannot tell an OLDER tick
-- arriving late (an async retry, a delayed invoke) from a derivation that lost
-- rows. Nothing was damaged (rolled back).
--
-- derived_through_ts = the newest candle (5-min candle START, epoch seconds) of
-- the run that wrote the row. db.write_register then:
--   * updates a stored row only when the incoming run is derived through the
--     same candle or a later one - an older run never overwrites a newer row;
--   * counts as "lost" only stored rows derived through the incoming run's
--     candle or earlier.
--
-- Existing rows: all 9 (session 2026-11-13) were last written by a full-day
-- run whose newest candle is 15:25 - so derived_through_ts = session_start
-- (IST midnight, epoch seconds) + 55,500 s = the 15:25 candle start. Plain
-- epoch arithmetic, no timezone functions. Then NOT NULL. Apply BEFORE deploying the code that writes it:
-- the code before it does not name the column, so with NOT NULL and no default
-- that code's inserts would fail - applied on a weekend, with no live runs.
--
-- APPLIED 2026-11-14 just before 23:08 IST to project nameless-mountain-15353651 /
-- database Algo (one transaction, lock_timeout 5s): 28 -> 29 columns, the 9 rows
-- (2026-11-13) set to 1794563700 (the 15:25 candle), NOT NULL in place. The code
-- that writes it (CodeSha256 +4qimC9c...) deployed at 23:08:22 IST, with the
-- previous build kept as Lambda version 4 (M3fvb4DI...).
--
-- ROLLBACK:
--   ALTER TABLE algo.playbook_alerts DROP COLUMN IF EXISTS derived_through_ts;
--   (and redeploy strategy-range-liquidity-sweep Lambda version 4, the code before it)
-- ============================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE algo.playbook_alerts
    ADD COLUMN IF NOT EXISTS derived_through_ts bigint;   -- newest candle of the run that wrote the row

UPDATE algo.playbook_alerts
   SET derived_through_ts = session_start + 55500          -- 15:25 candle start: the full-day runs that wrote them
 WHERE derived_through_ts IS NULL;

ALTER TABLE algo.playbook_alerts
    ALTER COLUMN derived_through_ts SET NOT NULL;

COMMIT;
