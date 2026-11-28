-- Migration: paper trading on the option (algo.option_paper_position + the rupee loss limit)
-- ============================================================================
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651), database
--         Algo, schema algo.  CONFIRM THE PROJECT ID BEFORE RUNNING - a second
--         Neon project carries an `algo` schema with the same table names and
--         incompatible columns.
--
-- Agreed with Amar 2026-11-28 ("Paper on the option"; build plan approved "Approve
-- as recommended": pattern-recorder/replay/out/p5-dhan-orders-2026-11-28/
-- paper-options-build-plan.md, git-ignored).
--
--   * algo.option_paper_position - one row per index paper position that bought its
--     option: the 1 ITM option bought (CE for a long, PE for a short), at Dhan's live
--     price when each step was applied. Written ONLY by order-manager, in the same
--     transaction as the index step. NO foreign key to algo.instrument_master: that
--     table cannot name a weekly contract (no expiry column, month-only symbols,
--     monthly reload - measured 2026-11-28).
--   * algo.risk_control.daily_loss_limit_rupees - the daily-loss limit in OPTION
--     RUPEES (Amar 2026-11-28: "Option rupees", "₹20,000"). It REPLACES the point-lot
--     limit: from this build risk-manager reads only the new column. The old
--     daily_loss_limit column stays for the history rows (NULL in new rows).
--
-- Additive: a new table and a new nullable column. Identical to order-manager/schema.sql
-- (the table) and risk-manager/schema.sql (the column).
--
-- APPLIED 2026-11-28 18:07:18 IST (a Saturday, no session), on Amar's "deploy now": the table empty, owner
-- neondb_owner; then the risk_control row set_at 1795869514 (4 / 8 / NULL / 20000, 'option paper deploy').
--
-- ROLLBACK:  DROP TABLE algo.option_paper_position;
--            ALTER TABLE algo.risk_control DROP COLUMN daily_loss_limit_rupees;

BEGIN;
SET LOCAL lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS algo.option_paper_position (
    position_id             bigint   PRIMARY KEY REFERENCES algo.position_master (position_id),
    underlying_security_id  text     NOT NULL,             -- '13' (NIFTY)
    option_type             text     NOT NULL CHECK (option_type IN ('CE', 'PE')),
    depth_itm               integer  NOT NULL CHECK (depth_itm >= 0),   -- 1 (Amar 2026-11-28, "1 ITM")
    -- the contract held now (a sweep re-entry buys a new one; every fill carries its own contract too)
    option_security_id      text     NOT NULL,             -- Dhan's id, from the option chain
    exchange_segment        text     NOT NULL,             -- raw Dhan code, 'NSE_FNO'
    trading_symbol          text     NOT NULL,             -- e.g. 'NIFTY 01DEC 25750 PE' (for reading only)
    strike                  numeric  NOT NULL,
    expiry_ts               bigint   NOT NULL,             -- IST midnight of the expiry date
    lot_size                integer  NOT NULL CHECK (lot_size > 0),
    buy_qty                 integer  NOT NULL DEFAULT 0 CHECK (buy_qty >= 0),    -- units, cumulative
    buy_value               numeric  NOT NULL DEFAULT 0,  -- rupees paid, cumulative
    sell_qty                integer  NOT NULL DEFAULT 0 CHECK (sell_qty >= 0),
    sell_value              numeric  NOT NULL DEFAULT 0,  -- rupees received, cumulative
    open_units              integer  NOT NULL DEFAULT 0 CHECK (open_units >= 0),
    charges_est             numeric  NOT NULL DEFAULT 0,  -- estimated per order, rates unverified
    realized_rupees         numeric  NOT NULL DEFAULT 0,  -- gross: received - cost of the units sold
    fills                   jsonb    NOT NULL,            -- [{kind, candle_ts, read_at, side, units, price, option_security_id, strike, expiry_ts, charges}]
    created_at              bigint   NOT NULL,
    updated_at              bigint   NOT NULL,
    CONSTRAINT option_paper_position_units CHECK (open_units = buy_qty - sell_qty)
);

ALTER TABLE algo.risk_control
    ADD COLUMN IF NOT EXISTS daily_loss_limit_rupees numeric CHECK (daily_loss_limit_rupees > 0);

COMMIT;
