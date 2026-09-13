-- daily-market-sentiment : schema addition
--
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651),
--         database Algo, schema algo.
--
-- One table. candle_daily already exists and is written by this function too.
--
-- Conventions (see CLAUDE.md):
--   * every time value is epoch seconds, bigint. No timestamptz, ever.
--   * (security_id, instrument_type) is instrument identity; security_id is text.
--   * numeric for prices, bigint for volume.

BEGIN;

-- ---------------------------------------------------------------------------
-- daily_market_sentiment : the daily read, computed from completed daily
-- candles by the shared market-classifier layer. It is NO LONGER a port of
-- trading-algo/helpers/sentiment_builder.py - see the classification block.
--
-- GRAIN. trade_date is the session the row describes - the newest COMPLETED
-- daily candle, i.e. yesterday, because Dhan's daily endpoint lags a session
-- and today's candle does not exist at 10:00.
--
-- Six columns are the exception and carry TODAY's values:
--     price, expected_move, upper_volatility, lower_volatility  - from today's open
--     min15_high, min15_low                                     - today's 09:15-09:30 range
-- Everything else describes trade_date.
--
-- WHAT IS DELIBERATELY NOT HERE. Anything recomputable from candle_daily that
-- nothing else in this row depends on - pivots (a pure function of
-- pd_high/pd_low/pd_close), atr_14, adr10. The indicators that ARE stored are
-- the ones the classification actually reads, so the row explains why its own
-- regime and score came out as they did.
--
-- NO OPTION DATA FEEDS THE CLASSIFICATION, on either frame. Dhan serves no
-- historical option chain, so PCR / IV-skew / straddle thresholds cannot be
-- measured until rows accumulate in intraday_market_sentiment. See the layer
-- README, "Revisit once sessions have accumulated".
--
-- sma200 is NOT NULL by intent: a null means fewer than 200 stored candles,
-- and a classifier comparing against a missing sma200 would have its
-- longer-SMA term contribute nothing while nothing raised - the score would
-- silently cap. The layer raises, build_daily_sentiment raises before it, and
-- this constraint is the third line of defence.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo.daily_market_sentiment (
    security_id       text    NOT NULL,
    instrument_type   text    NOT NULL,
    trade_date        bigint  NOT NULL,   -- IST midnight of the session described

    -- ---- classification ---------------------------------------------------
    -- Produced by the market-classifier layer on the daily frame, with the
    -- SAME rules intraday-market-sentiment runs on 5-minute candles. That is
    -- new: this table previously held a port of trading-algo's
    -- detect_market_regime / calculate_sentiment while the intraday path held
    -- a port of a different legacy builder, so `regime` and `score` were
    -- different measurements sharing a name across the two tables.
    --
    -- score IS NOT COMPARABLE ACROSS FRAMES WITHOUT max_score. The 5-minute
    -- frame carries a VWAP term this frame cannot - there is no session VWAP
    -- on a daily candle - so it scores out of 5 and this out of 4. Stored so a
    -- reader normalises rather than assumes.
    bias              text    NOT NULL,   -- bullish | bearish | range-bound
    structure         text    NOT NULL,   -- trending | sideways | transitional
    regime            text    NOT NULL,   -- trending | sideways | volatile-expansion
    volatility        text,               -- low | normal | high; null without VIX
    score             integer NOT NULL,
    max_score         integer NOT NULL,
    confidence        numeric NOT NULL,

    -- the swing read behind `structure`, over the recent DAILY bars rather
    -- than one session - which is what identifies a broken trend and the
    -- broader bias. swing_high/low are null until two swings of each kind
    -- confirm; structure_determined says so explicitly rather than leaving a
    -- caller to infer it from the nulls.
    swing_direction        text    NOT NULL,   -- up | down | none
    swing_high             numeric,
    swing_low              numeric,
    structure_determined   boolean NOT NULL,

    -- the volatility read behind `regime`
    range_used             numeric,            -- day range / expected move
    volatility_expanding   boolean NOT NULL,

    -- today's open against pd_close, in percent - the gap up/down. Carried,
    -- NOT scored: it describes the open, and a gap that filled by 10:00 should
    -- not keep voting on the daily bias all day.
    gap_pct                numeric,

    -- previous-session price context
    pd_high           numeric NOT NULL,
    pd_low            numeric NOT NULL,
    pd_close          numeric NOT NULL,

    -- the inputs the classification above was computed from
    rsi               numeric,
    sma9              numeric,
    sma50             numeric,
    sma100            numeric,
    sma200            numeric NOT NULL,
    prev_volume       bigint  NOT NULL,
    avg_volume_50     bigint  NOT NULL,
    vix               numeric,            -- previous session's INDIA VIX close

    -- today-grain (see GRAIN note above)
    price             numeric,            -- today's open
    expected_move     numeric,            -- price x (vix/100) / sqrt(252) x K
    upper_volatility  numeric,            -- price + expected_move
    lower_volatility  numeric,            -- price - expected_move
    min15_high        numeric,            -- today's 09:15-09:30 high
    min15_low         numeric,            -- today's 09:15-09:30 low

    created_at        bigint  NOT NULL,

    PRIMARY KEY (security_id, instrument_type, trade_date),
    FOREIGN KEY (security_id, instrument_type)
        REFERENCES algo.instrument_master (security_id, instrument_type)
);

COMMIT;
