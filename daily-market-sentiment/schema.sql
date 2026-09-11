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
-- candles. Ported from trading-algo/helpers/sentiment_builder.py.
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
-- the ones detect_market_regime() and calculate_sentiment() actually read, so
-- the row explains why its own regime and score came out as they did.
--
-- sma200 is NOT NULL by intent: a null means fewer than 200 stored candles,
-- and detect_market_regime() silently returns TRANSITION when it compares
-- against a missing sma200 rather than raising. The handler raises first; this
-- constraint is the second line of defence.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo.daily_market_sentiment (
    security_id       text    NOT NULL,
    instrument_type   text    NOT NULL,
    trade_date        bigint  NOT NULL,   -- IST midnight of the session described

    -- classification
    bias              text    NOT NULL,   -- bullish | neutral | bearish
    structure         text    NOT NULL,   -- trending | sideways | transitional
    regime            text    NOT NULL,   -- the two combined
    score             integer NOT NULL,
    confidence        numeric NOT NULL,

    -- previous-session price context
    pd_high           numeric NOT NULL,
    pd_low            numeric NOT NULL,
    pd_close          numeric NOT NULL,

    -- the inputs the classification above was computed from
    rsi               numeric,
    sma20             numeric,
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
