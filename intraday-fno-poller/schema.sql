-- intraday-fno-poller : schema addition
--
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651),
--         database Algo, schema algo.
--         CONFIRM THE PROJECT ID FIRST - a second project carries an `algo`
--         schema with the same table names and incompatible column types.
--
-- Two NEW tables. Purely additive: nothing existing is altered, and nothing
-- else writes to either of these.
--
-- Conventions (see CLAUDE.md):
--   * every time value is epoch seconds, bigint. No timestamptz, ever -
--     expiry dates included, stored as IST midnight of the expiry day.
--   * (security_id, instrument_type) is instrument identity; security_id is text.
--   * numeric for prices, bigint for open interest and volume.
--
-- ROLLBACK:  DROP TABLE algo.fno_5m_leg; DROP TABLE algo.fno_5m_snapshot;

BEGIN;

-- ---------------------------------------------------------------------------
-- fno_5m_snapshot : one row per five-minute tick, ~72 a session.
--
-- THE FAST COMPANION OF algo.intraday_fno_data. That table is the 15-minute
-- snapshot and is untouched; this one is the five-minute reading of the F&O
-- side only. The near_* / mth_* aggregate columns carry the SAME NAMES and the
-- same meaning as on intraday_fno_data, so one query joins the two grains:
--
--     SELECT f.*, s.near_pcr_oi AS ref_near_pcr_oi
--     FROM algo.fno_5m_snapshot f
--     LEFT JOIN LATERAL (
--         SELECT * FROM algo.intraday_fno_data s
--         WHERE s.security_id = f.security_id AND s.instrument_type = f.instrument_type
--           AND s.snapshot_ts <= f.snapshot_ts
--         ORDER BY s.snapshot_ts DESC LIMIT 1) s ON true;
--
-- NO REFERENCE POINTER IS STORED. The 15-minute reference is joined at read
-- time (above) rather than written, so this function does not depend on the
-- slow chain's timing. The 15-minute delta of anything here is the row three
-- ticks back, also derived at read time.
--
-- GRAIN. snapshot_ts is the timestamp of the NEWEST BAR DHAN RETURNED at the
-- moment of the run - the one still forming. It lands on the same grid as
-- candle_5min.candle_ts and, at a 15-minute mark, equals the slow row's
-- snapshot_ts. The last tick of a day is stamped 15:25 (there is no 15:30 bar).
--
-- TWO BARS, AND THEY ARE NOT THE SAME BAR.
--   * fut_price, fut_oi, spot are LIVE - read off the forming bar.
--   * idx_bar_* and fut_bar_* are the last CLOSED bar. Their volume and OI are
--     final. THESE are what a five-minute reading compares against a reference;
--     the forming bar's own volume and OI are seconds old and near-empty.
--
-- fut_bar_oi_change is the closed bar's open interest minus the bar BEFORE it,
-- both taken from the same Dhan response. It does not depend on a previous row,
-- so a missed tick cannot corrupt it. It is NULL when the two bars are not
-- adjacent, and for the session's first bar. It is the signal the logged
-- observations turned on: OI RISING at a low (shorts pressing) then FALLING on
-- the thrust (short covering).
--
-- EVERY *_change / *_change_pct IS VERSUS prev_snapshot_ts, the previous tick
-- ON THE SAME IST DAY. Unlike the 15-minute row, the first tick of a session has
-- NO baseline and its deltas are NULL: a "five-minute" delta that silently
-- covers sixteen hours is a trap. A missed tick widens the window instead of
-- nulling it, which is why prev_snapshot_ts is stored.
--
-- WHY OI DELTAS CAN BE NULL. If the previous tick's contract differs from this
-- tick's - a monthly futures roll, or an option expiry roll - its price and OI
-- belong to something that no longer exists here, and a percentage between the
-- two reads exactly like a real collapse in open interest (measured 2026-09-30
-- on the slow row: +146%). The poller leaves those deltas NULL across a roll.
--
-- NOT HERE, ON PURPOSE: VIX, SMA/RSI, breadth, the opening range, day high/low,
-- VWAP, and any classification. Those are on intraday_fno_data /
-- intraday_sentiments at 15-minute grain.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo.fno_5m_snapshot (
    security_id            text    NOT NULL,   -- the underlying index
    instrument_type        text    NOT NULL,
    snapshot_ts            bigint  NOT NULL,   -- newest bar returned (forming)
    captured_at            bigint  NOT NULL,   -- when the run actually fired
    prev_snapshot_ts       bigint,             -- baseline for every *_change*

    -- ---- underlying index -------------------------------------------------
    spot                   numeric NOT NULL,   -- live: the forming bar's close
    chain_spot             numeric,            -- data.last_price from the chain
    spot_change_pct        numeric,
    idx_bar_ts             bigint  NOT NULL,   -- last CLOSED 5-min bar
    idx_bar_volume         bigint  NOT NULL,

    -- ---- current-month future ---------------------------------------------
    fut_security_id        text    NOT NULL,
    fut_symbol             text    NOT NULL,
    fut_price              numeric NOT NULL,   -- live
    fut_oi                 bigint  NOT NULL,   -- live
    basis                  numeric NOT NULL,   -- fut_price - spot
    basis_pct              numeric NOT NULL,
    fut_price_change_pct   numeric,
    fut_oi_change          bigint,
    fut_oi_change_pct      numeric,
    fut_bar_ts             bigint  NOT NULL,   -- last CLOSED 5-min bar
    fut_bar_volume         bigint  NOT NULL,
    fut_bar_oi             bigint  NOT NULL,
    fut_bar_oi_change      bigint,             -- closed bar's OI - the bar before's

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
    near_ce_oi_change      bigint,
    near_pe_oi_change      bigint,
    near_ce_oi_change_pct  numeric,
    near_pe_oi_change_pct  numeric,
    near_max_oi_call       numeric,            -- strike carrying most call OI
    near_max_oi_put        numeric,
    near_max_pain          numeric,
    near_ce_iv             numeric,
    near_pe_iv             numeric,
    near_iv_skew           numeric,            -- pe_iv - ce_iv

    -- ---- monthly expiry ----------------------------------------------------
    -- Rolls to the NEXT monthly when the nearest expiry is itself its month's
    -- last, so near_expiry_ts and mth_expiry_ts are never equal.
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
    mth_ce_oi_change       bigint,
    mth_pe_oi_change       bigint,
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
-- fno_5m_leg : the raw legs, NEAREST EXPIRY ONLY.
--
-- 5 strikes centred on ATM (ATM, +-1, +-2) x CE and PE = 10 rows a tick, 720 a
-- session. The same columns and key as algo.option_chain_snapshot, but a
-- SEPARATE TABLE: both writers would otherwise upsert the same snapshot_ts key
-- at every 15-minute mark (last writer wins), and any reader assuming "10 legs
-- per snapshot" would suddenly see 30.
--
-- This is the RAW leg as Dhan returned it - bid/ask included - so a tick can be
-- replayed rather than merely summarised. What is NOT here is any derived
-- figure: no straddle, no PCR. Those live on the row above, computed over a
-- wider window than these ten rows could support.
--
-- implied_volatility AND THE GREEKS ARE NULL WHERE DHAN SENT 0. Measured
-- 2026-09-12: an illiquid deep-ITM leg returns implied_volatility 0 with all
-- four greeks 0. That is "not computed", not "zero volatility", and stored as 0
-- it silently poisons any average or skew taken across legs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo.fno_5m_leg (
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
    CONSTRAINT fno_5m_leg_side CHECK (option_type IN ('CE', 'PE'))
);

COMMIT;
