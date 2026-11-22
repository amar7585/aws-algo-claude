-- order-manager : schema addition (the paper position record)
--
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651),
--         database Algo, schema algo.
--         CONFIRM THE PROJECT ID FIRST - a second project carries an `algo`
--         schema with the same table names and incompatible column types.
--
-- One NEW table, for a fresh database. The deployed database gets it from
-- migrations/2026-11-22-position-master.sql; the result must stay identical to
-- this file. Purely additive: nothing existing is altered.
--
-- ONLY order-manager writes it (Amar 2026-11-22: one common order manager, "like
-- what we have in algo_trading app just dont save order directly save the
-- position in position master"). In paper mode no order is saved: each executed
-- step updates the one position row. The shape follows trading-algo's
-- position_master / paper_trade_master.
--
-- Conventions (see CLAUDE.md):
--   * every time value is epoch seconds, bigint. No timestamptz, ever.
--     entry_ts / last_update / t1_booked_ts / reentry_ts / closed_ts are 5-min
--     candle STARTS; session_start is IST midnight of the session; created_at /
--     updated_at / message_sent_at are wall clock.
--   * exchange_segment columns hold raw Dhan segment codes (IDX_I, NSE_FNO, ...).
--   * (security_id, instrument_type) is instrument identity; security_id is text.
--   * numeric for prices.
--   * QUANTITIES ARE UNITS (Amar 2026-11-22, "Units now"): lots x lot_size, as
--     Dhan's position API reports them. The paper position is on the NIFTY index
--     price, whose own lot_units is 1, so lot_size is the near-month NIFTY
--     future's lot_units at entry (65 on 2026-11-22), stored per position.
--     realized_profit / unrealized_profit are index points x units.
--
-- ROLLBACK:  DROP TABLE algo.position_master;

BEGIN;

CREATE TABLE IF NOT EXISTS algo.position_master (
    position_id                 bigint   GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    broker_id                   text     NOT NULL DEFAULT 'paper',
    trade_mode                  text     NOT NULL CHECK (trade_mode IN ('PAPER')),   -- 'LIVE' comes with the Dhan-phase migration
    playbook                    text     NOT NULL,   -- the deciding function, e.g. strategy-range-liquidity-sweep
    signal_key                  text     NOT NULL,   -- the playbook's own idea id (the first one, when duplicates merge)
    session_start               bigint   NOT NULL,   -- IST midnight of the session
    security_id                 text     NOT NULL,   -- the traded instrument (paper: the NIFTY index, '13')
    instrument_type             text     NOT NULL,   -- 'INDEX' in paper mode
    exchange_segment            text     NOT NULL,   -- raw Dhan code ('IDX_I' in paper mode)
    trading_symbol              text     NOT NULL,
    underlying_security_id      text     NOT NULL,   -- = the traded instrument in paper mode
    underlying_instrument_type  text     NOT NULL,
    underlying_exchange_segment text     NOT NULL,
    position_type               text     NOT NULL CHECK (position_type IN ('LONG', 'SHORT')),
    product_type                text     NOT NULL DEFAULT 'INTRADAY',
    entry_ts                    bigint   NOT NULL,   -- the signal candle (START)
    lot_size                    integer  NOT NULL CHECK (lot_size > 0),   -- the near-month NIFTY future's lot_units at entry
    buy_qty                     integer  NOT NULL DEFAULT 0 CHECK (buy_qty >= 0),    -- units, cumulative (as legacy / Dhan)
    buy_avg                     numeric,
    sell_qty                    integer  NOT NULL DEFAULT 0 CHECK (sell_qty >= 0),   -- units, cumulative
    sell_avg                    numeric,
    open_avg                    numeric,             -- average price of the units still open (a re-entry resets it)
    stop_loss                   numeric,
    tsl_stop_loss               numeric,             -- a moved stop (trend: to entry, then the trail)
    t1_target                   numeric,
    target                      numeric,
    risk_pts                    numeric,             -- entry - stop, index points (for R)
    status                      text     NOT NULL CHECK (status IN ('OPEN', 'WATCHING', 'CLOSED', 'REFUSED')),
    refused_reason              text,
    realized_profit             numeric  NOT NULL DEFAULT 0,   -- index points x units
    unrealized_profit           numeric,             -- the risk manager's mark (from P2b)
    t1_booked_ts                bigint,
    reentry_ts                  bigint,
    closed_ts                   bigint,
    exit_reason                 text,
    last_update                 bigint   NOT NULL,   -- candle (START) of the last step applied
    message_sent_at             bigint,              -- wall clock of the last message sent
    created_at                  bigint   NOT NULL,
    updated_at                  bigint   NOT NULL,

    -- One position per playbook, entry candle and direction: two alerts on the same
    -- candle (10 in the 2026-11-22 limits replay) are the same trade.
    CONSTRAINT position_master_one_per_entry
        UNIQUE (security_id, instrument_type, playbook, entry_ts, position_type),
    -- The balance agrees with the status.
    CONSTRAINT position_master_balance_status
        CHECK ((status = 'OPEN' AND buy_qty <> sell_qty) OR (status <> 'OPEN' AND buy_qty = sell_qty)),
    CONSTRAINT position_master_refused_reason
        CHECK ((status = 'REFUSED') = (refused_reason IS NOT NULL)),
    FOREIGN KEY (security_id, instrument_type)
        REFERENCES algo.instrument_master (security_id, instrument_type)
);

-- The risk manager reads what is open; the per-session counts read by session.
CREATE INDEX IF NOT EXISTS position_master_live
    ON algo.position_master (status) WHERE status IN ('OPEN', 'WATCHING');
CREATE INDEX IF NOT EXISTS position_master_session
    ON algo.position_master (session_start, playbook);

COMMIT;
