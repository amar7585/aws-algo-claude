-- Migration: split algo.intraday_market_sentiment into measurement + judgement
-- ============================================================================
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651), database
--         Algo, schema algo.  CONFIRM THE PROJECT ID BEFORE RUNNING - a second
--         Neon project carries an `algo` schema with the same table names and
--         incompatible columns.
--
-- Strategy: MIGRATE & PRESERVE (chosen 2026-09-19). The existing snapshot rows
-- are kept: their measurement columns move to algo.intraday_fno_data and their
-- classification columns to algo.intraday_sentiments. The old table is RENAMED
-- (not dropped), so the migration is reversible.
--
-- RUN THE TWO CREATE-TABLE SCHEMAS FIRST, in this order, then this file:
--   1. intraday-market-sentiment/schema.sql   (creates algo.intraday_fno_data;
--                                               option_chain_snapshot is IF NOT
--                                               EXISTS, a no-op if it exists)
--   2. market-classifier/schema.sql           (creates algo.intraday_sentiments)
--
-- option_chain_snapshot is UNCHANGED and is not touched here.
-- ============================================================================

BEGIN;

-- 1. Measurement rows -> intraday_fno_data ----------------------------------
INSERT INTO algo.intraday_fno_data (
    security_id, instrument_type, snapshot_ts, captured_at, prev_snapshot_ts,
    spot, chain_spot, spot_change_pct, day_high, day_low, vwap, orb_high, orb_low,
    last_bar_volume, volume_vs_avg,
    fut_security_id, fut_symbol, fut_price, fut_oi, basis, basis_pct,
    fut_price_change_pct, fut_oi_change_pct,
    vix, vix_open, vix_day_high, vix_day_low, vix_change_pct,
    near_expiry_ts, near_atm_strike, near_ce_ltp, near_pe_ltp, near_straddle,
    near_straddle_pct, near_pcr_oi, near_pcr_volume, near_ce_oi_total,
    near_pe_oi_total, near_ce_oi_change_pct, near_pe_oi_change_pct,
    near_max_oi_call, near_max_oi_put, near_max_pain, near_ce_iv, near_pe_iv,
    near_iv_skew,
    mth_expiry_ts, mth_atm_strike, mth_ce_ltp, mth_pe_ltp, mth_straddle,
    mth_straddle_pct, mth_pcr_oi, mth_pcr_volume, mth_ce_oi_total,
    mth_pe_oi_total, mth_ce_oi_change_pct, mth_pe_oi_change_pct,
    mth_max_oi_call, mth_max_oi_put, mth_max_pain, mth_ce_iv, mth_pe_iv,
    mth_iv_skew,
    sma9, sma50, sma100, sma200, rsi,
    created_at
)
SELECT
    security_id, instrument_type, snapshot_ts, captured_at, prev_snapshot_ts,
    spot, chain_spot, spot_change_pct, day_high, day_low, vwap, orb_high, orb_low,
    last_bar_volume, volume_vs_avg,
    fut_security_id, fut_symbol, fut_price, fut_oi, basis, basis_pct,
    fut_price_change_pct, fut_oi_change_pct,
    vix, vix_open, vix_day_high, vix_day_low, vix_change_pct,
    near_expiry_ts, near_atm_strike, near_ce_ltp, near_pe_ltp, near_straddle,
    near_straddle_pct, near_pcr_oi, near_pcr_volume, near_ce_oi_total,
    near_pe_oi_total, near_ce_oi_change_pct, near_pe_oi_change_pct,
    near_max_oi_call, near_max_oi_put, near_max_pain, near_ce_iv, near_pe_iv,
    near_iv_skew,
    mth_expiry_ts, mth_atm_strike, mth_ce_ltp, mth_pe_ltp, mth_straddle,
    mth_straddle_pct, mth_pcr_oi, mth_pcr_volume, mth_ce_oi_total,
    mth_pe_oi_total, mth_ce_oi_change_pct, mth_pe_oi_change_pct,
    mth_max_oi_call, mth_max_oi_put, mth_max_pain, mth_ce_iv, mth_pe_iv,
    mth_iv_skew,
    sma9, sma50, sma100, sma200, rsi,
    created_at
FROM algo.intraday_market_sentiment
ON CONFLICT (security_id, instrument_type, snapshot_ts) DO NOTHING;

-- 2. Classification rows -> intraday_sentiments -----------------------------
INSERT INTO algo.intraday_sentiments (
    security_id, instrument_type, snapshot_ts, captured_at, prev_snapshot_ts,
    bias, structure, regime, volatility, score, max_score, confidence, buildup,
    swing_direction, swing_high, swing_low, structure_determined,
    range_used, session_elapsed, volatility_expanding, created_at
)
SELECT
    security_id, instrument_type, snapshot_ts, captured_at, prev_snapshot_ts,
    bias, structure, regime, volatility, score, max_score, confidence, buildup,
    swing_direction, swing_high, swing_low, structure_determined,
    range_used, session_elapsed, volatility_expanding, created_at
FROM algo.intraday_market_sentiment
ON CONFLICT (security_id, instrument_type, snapshot_ts) DO NOTHING;

-- 3. Archive the old wide table (reversible - not dropped) -------------------
ALTER TABLE algo.intraday_market_sentiment
    RENAME TO intraday_market_sentiment_pre_split;

COMMIT;

-- Verify (run after COMMIT):
--   SELECT
--     (SELECT count(*) FROM algo.intraday_market_sentiment_pre_split) AS old_rows,
--     (SELECT count(*) FROM algo.intraday_fno_data)                   AS fno_rows,
--     (SELECT count(*) FROM algo.intraday_sentiments)                 AS sentiment_rows;
--   -- old_rows should equal fno_rows and sentiment_rows.
