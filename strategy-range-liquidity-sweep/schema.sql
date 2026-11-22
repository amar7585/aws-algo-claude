-- strategy-range-liquidity-sweep : schema addition (the alert register)
--
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651),
--         database Algo, schema algo.
--         CONFIRM THE PROJECT ID FIRST - a second project carries an `algo`
--         schema with the same table names and incompatible column types.
--
-- One NEW table, for a fresh database. The deployed database got it from
-- migrations/2026-11-14-playbook-alerts.sql (applied 2026-11-14, shortly before 22:01 IST)
-- plus migrations/2026-11-14-playbook-alerts-derived-through.sql (derived_through_ts);
-- the result must stay identical to this file. Purely additive: nothing existing is altered,
-- and only this function writes it (register.py / db.write_register, when
-- REGISTER_ENABLED=on). Nothing reads it back - it is a record.
--
-- Conventions (see CLAUDE.md):
--   * every time value is epoch seconds, bigint. No timestamptz, ever.
--     alert_ts / status_ts / entry_ts and the ts inside touch_log / steps are
--     candle STARTS; session_start is IST midnight of the session.
--   * (security_id, instrument_type) is instrument identity; security_id is text.
--   * numeric for prices.
--
-- ROLLBACK:  DROP TABLE algo.playbook_alerts;

BEGIN;

CREATE TABLE IF NOT EXISTS algo.playbook_alerts (
    security_id      text     NOT NULL,
    instrument_type  text     NOT NULL,
    playbook         text     NOT NULL,   -- the writing function, e.g. strategy-range-liquidity-sweep
    logic            text     NOT NULL,   -- the rule set: 'new' only since 2026-11-19 ('current' removed)
    side             text     NOT NULL,   -- the side of the level touched: high | low
    level_name       text     NOT NULL,   -- 15min high, PDL, 1Hr low, ...
    alert_ts         bigint   NOT NULL,   -- the alert candle: first touch of this alert
    session_start    bigint   NOT NULL,   -- IST midnight of the session
    derived_through_ts bigint NOT NULL,   -- newest candle of the run that wrote the row (an older run never overwrites it)
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
    t1_price         numeric,             -- the range midpoint, where 2 of 3 lots are booked (since 2026-11-22; NULL = entry past it)
    t2_price         numeric,
    risk_reward      numeric,
    grade            text,
    outcome          text,                -- STOP HIT | TARGET REACHED | WATCH ENDED | CLOSED AT 15:15 (since 2026-11-20) | open | watching | re-entered
    steps            jsonb    NOT NULL,   -- [{kind, ts, price, reason}] after the entry; kind T1 REACHED since 2026-11-22 (never the last step)
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
