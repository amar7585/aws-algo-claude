-- Migration: the kill switch and the limits (algo.risk_control)
-- ============================================================================
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651), database
--         Algo, schema algo.  CONFIRM THE PROJECT ID BEFORE RUNNING - a second
--         Neon project carries an `algo` schema with the same table names and
--         incompatible columns.
--
-- Agreed with Amar 2026-11-22 / 11-23 (plan "B: paper first"; P2b build plan
-- approved: side-session-notes/.../p2b-build-plan-2026-11-23.md). Append-only:
-- the newest row with set_at <= now is in force. The kill switch resets each
-- morning (on for the session it is set in, else the next session). Limits NULL
-- = off until P3 writes O1 (max open 4 / max 8 trades per day / daily loss 500
-- point-lots).
--
-- Read ONLY by risk-manager. Written by Amar's own INSERTs (risk-manager README)
-- and by this migration's seed row.
--
-- Additive: a new table, nothing existing is altered. Identical to
-- risk-manager/schema.sql plus the seed row.
--
-- APPLIED 2026-11-23 15:32:05 IST (seed set_at 1795428125), after the close, on
-- Amar's "deploy now" - P2a's two-session live check skipped on his call.
--
-- ROLLBACK:  DROP TABLE algo.risk_control;

BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS algo.risk_control (
    set_at              bigint   PRIMARY KEY,
    kill_switch         boolean  NOT NULL,
    max_open            integer  CHECK (max_open > 0),
    max_trades_per_day  integer  CHECK (max_trades_per_day > 0),
    daily_loss_limit    numeric  CHECK (daily_loss_limit > 0),
    set_by              text     NOT NULL,
    reason              text     NOT NULL
);

-- The seed: kill switch off, no limits (P2b). P3 inserts O1.
INSERT INTO algo.risk_control (set_at, kill_switch, max_open, max_trades_per_day, daily_loss_limit, set_by, reason)
VALUES (extract(epoch FROM now())::bigint, false, NULL, NULL, NULL, 'P2b deploy', 'seed: kill switch off, limits off until P3');

COMMIT;
