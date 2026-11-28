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
-- Limits: NULL = no limit. The daily-loss limit is daily_loss_limit_rupees (option
-- rupees, Amar 2026-11-28: ₹20,000), compared with the day's option paper record
-- (algo.option_paper_position): cash so far + open options marked live. The older
-- daily_loss_limit (index points x lots, O1 = 500, 2026-11-22) is no longer read; it
-- stays in the history rows and is NULL in new ones.
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
    daily_loss_limit    numeric  CHECK (daily_loss_limit > 0),     -- index points x lots - HISTORY ONLY since 2026-11-28
    set_by              text     NOT NULL,
    reason              text     NOT NULL,
    -- The daily-loss limit in OPTION RUPEES (Amar 2026-11-28: "Option rupees", "₹20,000"): the day's option
    -- cash (received - paid - estimated charges) + every open option marked at its live price. It REPLACES
    -- daily_loss_limit: risk-manager reads only this column; new rows carry NULL in the old one.
    -- migrations/2026-11-28-option-paper.sql.
    daily_loss_limit_rupees numeric CHECK (daily_loss_limit_rupees > 0)
);

COMMIT;
