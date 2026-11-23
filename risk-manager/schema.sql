-- risk-manager : schema addition (the kill switch and the limits)
--
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651),
--         database Algo, schema algo.
--         CONFIRM THE PROJECT ID FIRST - a second project carries an `algo`
--         schema with the same table names and incompatible column types.
--
-- One NEW table, for a fresh database. The deployed database gets it from
-- migrations/2026-11-23-risk-control.sql; the result must stay identical to this
-- file. Purely additive.
--
-- APPEND-ONLY (Amar 2026-11-22, K-A): every change is a new row; the row in force
-- is the newest with set_at <= now. So a replay knows the limits in force at any
-- past tick. Amar flips the kill switch or changes a limit with ONE INSERT (the
-- statements are in risk-manager/README.md); nothing is ever updated or deleted.
--
-- THE KILL SWITCH RESETS EACH MORNING (Amar 2026-11-22 / 11-23): a row with
-- kill_switch = true is ON for the session it is set in - or, if set outside
-- session hours, for the next session: on for session S when set_at is after the
-- previous trading session's close (15:30 IST) and S is running. It needs no
-- reset row.
--
-- Limits: NULL = no limit. daily_loss_limit is in index points x LOTS (O1 = 500,
-- Amar 2026-11-22); the risk manager compares it with each position's realised +
-- marked profit divided by that position's lot_size (position_master holds units).
--
-- Conventions: every time value is epoch seconds (bigint); no timestamptz.
--
-- ROLLBACK:  DROP TABLE algo.risk_control;

BEGIN;

CREATE TABLE IF NOT EXISTS algo.risk_control (
    set_at              bigint   PRIMARY KEY,   -- wall clock when the row was written
    kill_switch         boolean  NOT NULL,
    max_open            integer  CHECK (max_open > 0),             -- open positions at once; NULL = no limit
    max_trades_per_day  integer  CHECK (max_trades_per_day > 0),   -- positions opened in a session (REFUSED not counted)
    daily_loss_limit    numeric  CHECK (daily_loss_limit > 0),     -- index points x lots, realised + marked
    set_by              text     NOT NULL,
    reason              text     NOT NULL
);

COMMIT;
