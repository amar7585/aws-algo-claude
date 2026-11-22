-- Migration: the paper position record (algo.position_master)
-- ============================================================================
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651), database
--         Algo, schema algo.  CONFIRM THE PROJECT ID BEFORE RUNNING - a second
--         Neon project carries an `algo` schema with the same table names and
--         incompatible columns.
--
-- Agreed with Amar 2026-11-22 (plan "B: paper first"; the P1 build plan approved:
-- side-session-notes/.../p1-build-plan-2026-11-22.md). Data model: follow
-- trading-algo's position_master - in paper mode no order is saved, each executed
-- step updates the one position row; quantities in UNITS (lots x the near-month
-- NIFTY future's lot size).
--
-- WHAT IT HOLDS. One row per position a playbook signalled (or REFUSED, when the
-- risk manager did not allow the entry). Written ONLY by order-manager. Read by
-- the risk manager from P2b. P1 creates it empty; nothing writes it until P2a.
--
-- Additive: a new table, nothing existing is altered. Identical to
-- order-manager/schema.sql (the fresh-database copy).
--
-- APPLIED 2026-11-22 shortly before 21:16 IST (Amar: "Deploy P1"), before the function was created
-- (21:16:44 IST). The project was nameless-mountain-15353651 / database Algo, in one transaction
-- with lock_timeout 5s. Target checked first: database Algo, instrument_master.security_id text,
-- the table absent.
-- Result:
--   * the table was created empty, owner neondb_owner, 39 columns;
--   * PK, UNIQUE position_master_one_per_entry, the FK to instrument_master and every CHECK;
--   * indexes position_master_live and position_master_session.
-- The deploy's two dry-run smoke invokes rolled back (0 rows), but consumed identity values 1-2.
--
-- ROLLBACK:  DROP TABLE algo.position_master;   (empty until P2a)

BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS algo.position_master (
    position_id                 bigint   GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    broker_id                   text     NOT NULL DEFAULT 'paper',
    trade_mode                  text     NOT NULL CHECK (trade_mode IN ('PAPER')),
    playbook                    text     NOT NULL,
    signal_key                  text     NOT NULL,
    session_start               bigint   NOT NULL,
    security_id                 text     NOT NULL,
    instrument_type             text     NOT NULL,
    exchange_segment            text     NOT NULL,
    trading_symbol              text     NOT NULL,
    underlying_security_id      text     NOT NULL,
    underlying_instrument_type  text     NOT NULL,
    underlying_exchange_segment text     NOT NULL,
    position_type               text     NOT NULL CHECK (position_type IN ('LONG', 'SHORT')),
    product_type                text     NOT NULL DEFAULT 'INTRADAY',
    entry_ts                    bigint   NOT NULL,
    lot_size                    integer  NOT NULL CHECK (lot_size > 0),
    buy_qty                     integer  NOT NULL DEFAULT 0 CHECK (buy_qty >= 0),
    buy_avg                     numeric,
    sell_qty                    integer  NOT NULL DEFAULT 0 CHECK (sell_qty >= 0),
    sell_avg                    numeric,
    open_avg                    numeric,
    stop_loss                   numeric,
    tsl_stop_loss               numeric,
    t1_target                   numeric,
    target                      numeric,
    risk_pts                    numeric,
    status                      text     NOT NULL CHECK (status IN ('OPEN', 'WATCHING', 'CLOSED', 'REFUSED')),
    refused_reason              text,
    realized_profit             numeric  NOT NULL DEFAULT 0,
    unrealized_profit           numeric,
    t1_booked_ts                bigint,
    reentry_ts                  bigint,
    closed_ts                   bigint,
    exit_reason                 text,
    last_update                 bigint   NOT NULL,
    message_sent_at             bigint,
    created_at                  bigint   NOT NULL,
    updated_at                  bigint   NOT NULL,
    CONSTRAINT position_master_one_per_entry
        UNIQUE (security_id, instrument_type, playbook, entry_ts, position_type),
    CONSTRAINT position_master_balance_status
        CHECK ((status = 'OPEN' AND buy_qty <> sell_qty) OR (status <> 'OPEN' AND buy_qty = sell_qty)),
    CONSTRAINT position_master_refused_reason
        CHECK ((status = 'REFUSED') = (refused_reason IS NOT NULL)),
    FOREIGN KEY (security_id, instrument_type)
        REFERENCES algo.instrument_master (security_id, instrument_type)
);

CREATE INDEX IF NOT EXISTS position_master_live
    ON algo.position_master (status) WHERE status IN ('OPEN', 'WATCHING');
CREATE INDEX IF NOT EXISTS position_master_session
    ON algo.position_master (session_start, playbook);

COMMIT;
