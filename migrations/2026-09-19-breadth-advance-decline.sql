-- Migration: add advance/decline breadth to the intraday measurement plane
-- ============================================================================
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651), database
--         Algo, schema algo.  CONFIRM THE PROJECT ID BEFORE RUNNING - a second
--         Neon project carries an `algo` schema with the same table names and
--         incompatible columns.
--
-- Adds:
--   1. algo.index_constituents          - the NIFTY 50 / NIFTY 500 rosters
--   2. nine breadth columns on algo.intraday_fno_data
--
-- Both are additive and nullable/new, so existing rows and the running
-- function are unaffected. Breadth runs as soon as the new code deploys: with
-- the rosters seeded it counts DB membership, and with the NIFTY 50 roster
-- empty it uses the built-in STATIC_NIFTY50 fallback - see the component README.
--
-- A fresh database gets all of this from intraday-market-sentiment/schema.sql
-- instead; this file is only for the already-deployed table.
--
-- APPLIED 2026-09-19 to project nameless-mountain-15353651 / database Algo:
-- both objects created, 92 intraday_fno_data rows untouched.
-- ============================================================================

BEGIN;

-- 1. The membership table ---------------------------------------------------
CREATE TABLE IF NOT EXISTS algo.index_constituents (
    index_name         text    NOT NULL,       -- 'NIFTY50' | 'NIFTY500'
    security_id        text    NOT NULL,
    instrument_type    text    NOT NULL,
    exchange_segment   text    NOT NULL,        -- raw Dhan segment code, e.g. NSE_EQ
    symbol             text    NOT NULL,        -- human-auditable ticker
    updated_at         bigint  NOT NULL,        -- when membership was last confirmed
    created_at         bigint  NOT NULL,

    PRIMARY KEY (index_name, security_id, instrument_type),
    FOREIGN KEY (security_id, instrument_type)
        REFERENCES algo.instrument_master (security_id, instrument_type)
);

-- 2. Breadth columns on the measurement row ---------------------------------
--    integer counts (<= 500), numeric ratio, all nullable. nifty_* is the
--    NIFTY 50, mkt_* the NIFTY 500; adv_dec_ratio is null when declines = 0.
ALTER TABLE algo.intraday_fno_data
    ADD COLUMN IF NOT EXISTS nifty_advances      integer,
    ADD COLUMN IF NOT EXISTS nifty_declines      integer,
    ADD COLUMN IF NOT EXISTS nifty_unchanged     integer,
    ADD COLUMN IF NOT EXISTS nifty_adv_dec_ratio numeric,
    ADD COLUMN IF NOT EXISTS mkt_advances        integer,
    ADD COLUMN IF NOT EXISTS mkt_declines        integer,
    ADD COLUMN IF NOT EXISTS mkt_unchanged       integer,
    ADD COLUMN IF NOT EXISTS mkt_adv_dec_ratio   numeric,
    ADD COLUMN IF NOT EXISTS mkt_sampled         integer;

COMMIT;

-- ============================================================================
-- Seeding the rosters - a SEPARATE, MANUAL step, run after this migration.
--
-- Dhan has no index-membership endpoint, so each roster is inserted from the
-- live published NIFTY 50 / NIFTY 500 lists, mapped to (security_id,
-- instrument_type) through instrument_master and VERIFIED before insert. The
-- template below resolves one symbol; the seed script fills the VALUES list
-- with the full roster and is checked against the live list on the seed date.
--
-- The exchange_segment and security_id are taken FROM instrument_master rather
-- than typed in, so identity cannot drift from what the loader stores.
--
--   INSERT INTO algo.index_constituents
--       (index_name, security_id, instrument_type, exchange_segment, symbol,
--        updated_at, created_at)
--   SELECT 'NIFTY50', im.security_id, im.instrument_type, im.exchange_segment,
--          im.trading_symbol, :asof, :asof
--   FROM algo.instrument_master im
--   WHERE im.instrument_type = 'EQUITY'
--     AND im.exchange_segment = 'NSE_EQ'
--     AND im.trading_symbol IN ( 'RELIANCE', 'HDFCBANK', ... )   -- the 50
--   ON CONFLICT (index_name, security_id, instrument_type) DO NOTHING;
--
-- Verify each roster after seeding:
--   SELECT index_name, count(*) FROM algo.index_constituents GROUP BY index_name;
--
-- SEEDED 2026-09-19 from the live niftyindices.com rosters: NIFTY50 = 50,
-- NIFTY500 = 498. The published NIFTY 500 carried 501 symbols; 3 could not be
-- seeded and were excluded:
--   * DUMMYHEG - an NSE index-maintenance placeholder, not a tradable scrip.
--   * HFCL (Dhan sid 21954), HEG (Dhan sid 7368) - real equities currently in
--     the BE (Trade-to-Trade) series, which instrument-master-loader drops
--     (its EQUITY rule keeps SEM_SERIES='EQ' only), so the FK to
--     instrument_master cannot be satisfied. Widening the loader to capture
--     BE-series members is tracked as a separate task; when done, top the
--     NIFTY500 roster up to its full published membership.
-- ============================================================================
