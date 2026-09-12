-- intraday-market-sentiment : schema addition
--
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651),
--         database Algo, schema algo.
--
-- Two tables. Nothing here is written by any other function.
--
-- Conventions (see CLAUDE.md):
--   * every time value is epoch seconds, bigint. No timestamptz, ever -
--     expiry dates included, stored as IST midnight of the expiry day.
--   * (security_id, instrument_type) is instrument identity; security_id is text.
--   * numeric for prices, bigint for open interest and volume.

BEGIN;

-- ---------------------------------------------------------------------------
-- intraday_market_sentiment : one row per snapshot, 25 a session.
--
-- GRAIN. snapshot_ts is the timestamp of the last CLOSED 5-minute bar at the
-- moment of the run, not the run clock. Runs fire at 09:35, 09:50, 10:05 ...
-- 15:35, so snapshot_ts lands on 09:30, 09:45, 10:00 ... 15:15 - a clean
-- 15-minute grid - AND THEN 15:25.
--
-- The last one is not a bug. A session's final 5-minute bar is stamped 15:25
-- and there is no 15:30 bar (the market closes then, and a 15:30 stamp is
-- post-close data this system filters). So the 15:35 run describes 15:25, and
-- its deltas cover TEN minutes rather than fifteen. prev_snapshot_ts is what
-- tells a reader that, which is exactly why the window is stored rather than
-- assumed. Verified against the 2026-09-11 session: 75 bars, 09:15 to 15:25.
--
-- Three things follow from the grain and all of them matter:
--   * a re-run at 09:36 overwrites its own row by primary key instead of
--     writing a second, near-identical one;
--   * the previous-snapshot lookup is exact rather than approximate;
--   * every stored value describes a bar that has finished forming, so no
--     column here is ever partial (unlike candle_5min, which stores the
--     in-progress bucket on purpose).
-- captured_at holds the actual run time, so the ~5 minute lag stays visible.
--
-- BOTH EXPIRIES LIVE ON ONE ROW. near_* is the nearest expiry, mth_* the
-- monthly. When the nearest expiry IS the last expiry of its month the two
-- would collide, so mth_* rolls to the NEXT monthly - meaning near_expiry_ts
-- and mth_expiry_ts are never equal. One row is therefore one complete read
-- of the moment. The cost is a wide table and a schema change if a third
-- expiry is ever wanted; that trade was made deliberately.
--
-- EVERY *_change_pct IS VERSUS prev_snapshot_ts, one consistent baseline.
-- On the first run of a day there is no earlier row for that day, so the
-- baseline is the PREVIOUS SESSION's 15:30 snapshot and the deltas span the
-- overnight gap. That is intended, and prev_snapshot_ts is stored so a reader
-- can see the window each delta actually covers rather than assuming 15
-- minutes. A missed run widens it the same way, with no special handling.
--
-- Open-relative values are NOT stored. spot, vix_open and the rest make the
-- first snapshot of a day recoverable from this table, so a "since open"
-- figure derives from the stored series instead of occupying columns here.
--
-- WHY OI DELTAS CAN BE NULL. If the previous row's expiry differs from this
-- row's, its OI totals describe a different contract and the percentage would
-- be arithmetic on unrelated numbers. The handler leaves them NULL across an
-- expiry roll rather than computing something meaningless.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo.intraday_market_sentiment (
    security_id            text    NOT NULL,   -- the underlying index
    instrument_type        text    NOT NULL,
    snapshot_ts            bigint  NOT NULL,   -- last CLOSED 5-min bar
    captured_at            bigint  NOT NULL,   -- when the run actually fired
    prev_snapshot_ts       bigint,             -- baseline for every *_change_pct

    -- ---- underlying index -------------------------------------------------
    spot                   numeric NOT NULL,   -- last closed 5-min close
    chain_spot             numeric,            -- data.last_price from the chain
    spot_change_pct        numeric,
    day_high               numeric NOT NULL,
    day_low                numeric NOT NULL,
    vwap                   numeric,
    orb_high               numeric,            -- 09:15-09:30 high
    orb_low                numeric,            -- 09:15-09:30 low

    -- ---- current-month future ---------------------------------------------
    fut_security_id        text    NOT NULL,
    fut_symbol             text    NOT NULL,
    fut_price              numeric NOT NULL,
    fut_oi                 bigint  NOT NULL,
    basis                  numeric NOT NULL,   -- fut_price - spot
    basis_pct              numeric NOT NULL,
    fut_price_change_pct   numeric,
    fut_oi_change_pct      numeric,
    buildup                text,               -- see sentiment.py

    -- ---- india vix ---------------------------------------------------------
    vix                    numeric,
    vix_open               numeric,
    vix_day_high           numeric,
    vix_day_low            numeric,
    vix_change_pct         numeric,

    -- ---- nearest expiry ----------------------------------------------------
    near_expiry_ts         bigint  NOT NULL,
    near_atm_strike        numeric NOT NULL,
    near_ce_ltp            numeric,
    near_pe_ltp            numeric,
    near_straddle          numeric,
    near_straddle_pct      numeric,            -- straddle as % of spot
    near_pcr_oi            numeric,
    near_pcr_volume        numeric,
    near_ce_oi_total       bigint,
    near_pe_oi_total       bigint,
    near_ce_oi_change_pct  numeric,
    near_pe_oi_change_pct  numeric,
    near_max_oi_call       numeric,            -- strike carrying most call OI
    near_max_oi_put        numeric,
    near_max_pain          numeric,
    near_ce_iv             numeric,
    near_pe_iv             numeric,
    near_iv_skew           numeric,            -- pe_iv - ce_iv

    -- ---- monthly expiry ----------------------------------------------------
    mth_expiry_ts          bigint  NOT NULL,
    mth_atm_strike         numeric NOT NULL,
    mth_ce_ltp             numeric,
    mth_pe_ltp             numeric,
    mth_straddle           numeric,
    mth_straddle_pct       numeric,
    mth_pcr_oi             numeric,
    mth_pcr_volume         numeric,
    mth_ce_oi_total        bigint,
    mth_pe_oi_total        bigint,
    mth_ce_oi_change_pct   numeric,
    mth_pe_oi_change_pct   numeric,
    mth_max_oi_call        numeric,
    mth_max_oi_put         numeric,
    mth_max_pain           numeric,
    mth_ce_iv              numeric,
    mth_pe_iv              numeric,
    mth_iv_skew            numeric,

    created_at             bigint  NOT NULL,

    PRIMARY KEY (security_id, instrument_type, snapshot_ts),
    FOREIGN KEY (security_id, instrument_type)
        REFERENCES algo.instrument_master (security_id, instrument_type)
);

-- ---------------------------------------------------------------------------
-- option_chain_snapshot : the raw legs, NEAREST EXPIRY ONLY.
--
-- 5 strikes centred on ATM (ATM, +-1, +-2) x CE and PE = 10 rows a snapshot,
-- 250 a session. Deliberately narrow: the aggregates above are computed over
-- ATM +-20, but storing 41 strikes x 2 sides x 2 expiries every 15 minutes is
-- 16x this volume for data that is mostly far-OTM noise.
--
-- This is the RAW leg as Dhan returned it - bid/ask included - so a snapshot
-- can be replayed rather than merely summarised. What is NOT here is any
-- derived figure: no straddle, no PCR. Those live on the row above, computed
-- over a wider window than these ten rows could support, and duplicating them
-- per leg would invite two answers to the same question.
--
-- implied_volatility AND THE GREEKS ARE NULL WHERE DHAN SENT 0. Measured
-- 2026-09-12: an illiquid deep-ITM leg returns implied_volatility 0 with all
-- four greeks 0. That is "not computed", not "zero volatility", and stored as
-- 0 it silently poisons any average or skew taken across legs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo.option_chain_snapshot (
    security_id           text    NOT NULL,    -- the UNDERLYING, not the leg
    instrument_type       text    NOT NULL,
    snapshot_ts           bigint  NOT NULL,
    expiry_ts             bigint  NOT NULL,
    strike                numeric NOT NULL,
    option_type           text    NOT NULL,    -- CE | PE

    option_security_id    text,                -- the leg's own security_id
    last_price            numeric,
    oi                    bigint,
    previous_oi           bigint,
    volume                bigint,
    previous_volume       bigint,
    previous_close_price  numeric,
    average_price         numeric,
    implied_volatility    numeric,             -- NULL where Dhan sent 0
    delta                 numeric,
    theta                 numeric,
    gamma                 numeric,
    vega                  numeric,
    top_bid_price         numeric,
    top_bid_quantity      bigint,
    top_ask_price         numeric,
    top_ask_quantity      bigint,

    created_at            bigint  NOT NULL,

    PRIMARY KEY (security_id, instrument_type, snapshot_ts, expiry_ts,
                 strike, option_type),
    FOREIGN KEY (security_id, instrument_type)
        REFERENCES algo.instrument_master (security_id, instrument_type),
    CONSTRAINT option_chain_snapshot_side CHECK (option_type IN ('CE', 'PE'))
);

COMMIT;
