-- daily-market-sentiment : schema addition
--
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651),
--         database Algo, schema algo.
--
-- Two tables. candle_daily already exists and is written by this function too.
-- intraday_width_profile was added 2026-11-12 (migrations/2026-11-12-recent-
-- range-volatility.sql); it is at the end of this file.
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

-- ---------------------------------------------------------------------------
-- intraday_width_profile : what a 5-min candle's width NORMALLY is at each
-- clock slot, over the previous 20 sessions. Built once each morning (09:35) by
-- this function for that day's session; intraday-market-sentiment reads it on
-- every 15-min run to judge its last six closed candles against it, and
-- market-classifier's volatility test reads the result (agreed with Amar
-- 2026-11-12).
--
-- WHY PER CLOCK SLOT. A 5-min candle is not equally wide all day. Over 103
-- sessions (2026-06-15 .. 11-11) the median 09:15 candle was 4.7x a midday
-- one, 09:20-09:35 1.6-1.9x, 15:00 1.5x. Against one flat average every
-- morning would read as volatile.
--
-- WHY THE LAST 20 SESSIONS, NOT ALL OF THEM. A normal taken from every session
-- since June sat below this autumn's market: the median snapshot read 1.45x it
-- and a 2.0 line fired on 20% of rows. From the last 20 the median reads 1.2x
-- and 2.0 fires on 8% (measured 2026-11-12, 936 snapshots, 09-15 .. 11-12).
--
-- GRAIN. 75 rows per session, 09:15 .. 15:25. slot_ts is the slot's epoch ON
-- THE SESSION THE ROW IS FOR (that day's candle_ts), so a reader joins today's
-- candles to it directly and no clock-of-day value is stored. The window is
-- weekday sessions strictly before trade_date; a weekend special session (the
-- 2026-11-08 Muhurat Sunday) is not a normal session and is left out.
--
-- FROZEN ONCE WRITTEN. The row records what normal WAS for that session, so a
-- stored snapshot can be replayed against exactly the yardstick it was judged
-- by, even if candle history is later refilled.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo.intraday_width_profile (
    security_id        text     NOT NULL,
    instrument_type    text     NOT NULL,
    slot_ts            bigint   NOT NULL,   -- this slot on trade_date (= that day's candle_ts)
    trade_date         bigint   NOT NULL,   -- IST midnight of the session it is for
    normal_width_pct   numeric  NOT NULL,   -- median of (high - low) / close x 100 at this slot
    samples            integer  NOT NULL,   -- sessions that had a bar at this slot
    sessions           integer  NOT NULL,   -- sessions in the window (20)
    first_session      bigint   NOT NULL,   -- IST midnight of the oldest session used
    last_session       bigint   NOT NULL,   -- IST midnight of the newest (the previous session)
    created_at         bigint   NOT NULL,

    PRIMARY KEY (security_id, instrument_type, slot_ts),
    FOREIGN KEY (security_id, instrument_type)
        REFERENCES algo.instrument_master (security_id, instrument_type)
);

COMMIT;
