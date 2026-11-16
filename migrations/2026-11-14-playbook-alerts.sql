-- Migration: the alert register (algo.playbook_alerts)
-- ============================================================================
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651), database
--         Algo, schema algo.  CONFIRM THE PROJECT ID BEFORE RUNNING - a second
--         Neon project carries an `algo` schema with the same table names and
--         incompatible columns.
--
-- Agreed with Amar 2026-11-14 (register = "re-derive and record"; step-1 plan
-- approved after the 104-session replay; missed = T2, a push still beyond the
-- level at 15:00 = invalid).
--
-- WHAT IT HOLDS. One row per ALERT: a level a playbook watches, touched on one
-- side, under one rule set - with its retests (touch_log), what became of it
-- (status) and, when a trade was entered or blocked by the gate, the trade.
-- strategy-range-liquidity-sweep writes it (register.py / db.write_register)
-- on every run, rebuilding the whole session and upserting it after its
-- messages are sent. NOTHING READS IT BACK - it is a record for analysis and
-- for comparing with the alert / entry candles Amar marks on his charts.
--
-- Times are epoch seconds (bigint) - candle STARTS for alert_ts / status_ts /
-- entry_ts and the ts inside touch_log / steps; session_start is IST midnight
-- of the session (the same value the playbook's day_start uses).
--
-- status: watching | entered | blocked | missed | invalid | ended | covered
-- (see strategy-range-liquidity-sweep/register.py for each one's rule).
--
-- Additive: a new table, nothing existing is altered. The playbook writes only
-- when its REGISTER_ENABLED is "on", so the table can exist before the code
-- relies on it.
--
-- APPLIED 2026-11-14 shortly before 22:01 IST (before the code went live) to project nameless-mountain-15353651 / database
-- Algo (one transaction, lock_timeout 5s; target checked: database Algo,
-- instrument_master.security_id text): table created empty, owner
-- neondb_owner, 28 columns, PK + FK + status / direction checks, index
-- playbook_alerts_session. strategy-range-liquidity-sweep deployed right after
-- (CodeSha256 IBg6OIzl..., previous HsOac/jT... kept as Lambda version 1) and
-- REGISTER_ENABLED=on at 22:01 IST; smoke runs wrote 2026-11-13's 9 rows, equal
-- to the offline replay.
--
-- ROLLBACK:
--   DROP TABLE IF EXISTS algo.playbook_alerts;
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS algo.playbook_alerts (
    security_id      text     NOT NULL,
    instrument_type  text     NOT NULL,
    playbook         text     NOT NULL,   -- the writing function, e.g. strategy-range-liquidity-sweep
    logic            text     NOT NULL,   -- the rule set: current | new
    side             text     NOT NULL,   -- the side of the level touched: high | low
    level_name       text     NOT NULL,   -- 15min high, PDL, 1Hr low, ...
    alert_ts         bigint   NOT NULL,   -- the alert candle: first touch of this alert
    session_start    bigint   NOT NULL,   -- IST midnight of the session
    level_price      numeric  NOT NULL,
    touches          integer  NOT NULL,   -- 1 + retests while the alert was watching
    touch_log        jsonb    NOT NULL,   -- [{ts, notes: [why that touch did / did not give an entry]}]
    status           text     NOT NULL
        CHECK (status IN ('watching', 'entered', 'blocked', 'missed', 'invalid', 'ended', 'covered')),
    status_ts        bigint,              -- the candle the status was reached on (NULL while watching)
    reason           text,                -- why, in the playbook's words
    target_price     numeric,             -- the T2 that marks a miss (opposite side of the opening range)
    direction        text     CHECK (direction IN ('long', 'short')),   -- entered / blocked rows
    entry_ts         bigint,              -- the entry candle (the signal is at its close)
    entry_price      numeric,             -- the playbook's entry (the close back inside)
    signal_close     numeric,             -- the entry candle's close (a market fill at the signal)
    stop_price       numeric,
    t1_price         numeric,
    t2_price         numeric,
    risk_reward      numeric,
    grade            text,
    outcome          text,                -- STOP HIT | TARGET REACHED | WATCH ENDED | open | watching | re-entered
    steps            jsonb    NOT NULL,   -- [{kind, ts, price, reason}] after the entry
    first_seen_at    bigint   NOT NULL,   -- the run that first wrote the row
    updated_at       bigint   NOT NULL,   -- the latest run that rewrote it

    PRIMARY KEY (security_id, instrument_type, playbook, logic, side, level_name, alert_ts),
    FOREIGN KEY (security_id, instrument_type)
        REFERENCES algo.instrument_master (security_id, instrument_type)
);

-- The per-run statement scopes on (instrument, playbook, session).
CREATE INDEX IF NOT EXISTS playbook_alerts_session
    ON algo.playbook_alerts (security_id, instrument_type, playbook, session_start);

COMMIT;
