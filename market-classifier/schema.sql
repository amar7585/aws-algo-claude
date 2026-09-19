-- market-classifier : schema addition
--
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651),
--         database Algo, schema algo.
--
-- One table. Written only by the market-classifier function.
--
-- Conventions (see CLAUDE.md):
--   * every time value is epoch seconds, bigint. No timestamptz, ever.
--   * (security_id, instrument_type) is instrument identity; security_id is text.
--   * numeric for prices.

BEGIN;

-- ---------------------------------------------------------------------------
-- intraday_sentiments : one JUDGEMENT row per snapshot, 25 a session.
--
-- THE OTHER HALF OF THE SPLIT. algo.intraday_fno_data holds what was measured;
-- this holds what the market-classifier layer decided FROM it - regime,
-- structure, bias, buildup and the swing/volatility reads. The two join on
-- (security_id, instrument_type, snapshot_ts): exactly one fno-data row and one
-- sentiments row per snapshot, written in that order by two functions
-- (intraday-market-sentiment measures and invokes market-classifier, which
-- classifies). Splitting the judgement out is what lets the classifier be its
-- own function and pattern-detector read the decision without re-deriving it.
--
-- ONE RULE SET, BOTH FRAMES. The layer's classify() that fills this is the same
-- one daily-market-sentiment runs on daily candles, so a row here and a daily
-- row are on one scale. max_score is stored because the 5-minute frame carries
-- a VWAP term the daily frame cannot (max_score 5 here, 4 there) - a reader
-- normalises rather than assumes.
--
-- NO OPTION TERM FEEDS THE SCORE YET. Dhan serves no historical option chain,
-- so PCR / IV-skew / straddle thresholds cannot be measured until fno-data rows
-- accumulate; scoring them on invented numbers would make `bias` mean one thing
-- before recalibration and another after. When the rows exist the agreed shape
-- is a separate options overlay, not extra terms folded into `score`.
--
-- buildup IS A JUDGEMENT LABEL, not a measurement. It is the four-quadrant read
-- of fut_price_change_pct against fut_oi_change_pct (LONG_BUILDUP, SHORT_BUILDUP,
-- LONG_UNWINDING, SHORT_COVERING, or FLAT within the epsilon band), derived here
-- from the fno-data deltas rather than stored on the measurement row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo.intraday_sentiments (
    security_id            text    NOT NULL,   -- the underlying index
    instrument_type        text    NOT NULL,
    snapshot_ts            bigint  NOT NULL,   -- joins to intraday_fno_data
    captured_at            bigint  NOT NULL,   -- the run that measured, carried through
    prev_snapshot_ts       bigint,             -- the baseline behind the deltas judged

    -- ---- the classification ------------------------------------------------
    bias                   text    NOT NULL,   -- bullish | bearish | range-bound
    structure              text    NOT NULL,   -- trending | sideways | transitional
    regime                 text    NOT NULL,   -- trending | sideways | volatile-expansion
    volatility             text,               -- low | normal | high; null without VIX
    score                  integer NOT NULL,
    max_score              integer NOT NULL,
    confidence             numeric NOT NULL,

    -- ---- futures OI buildup (derived from the fno-data deltas) --------------
    buildup                text,               -- LONG_BUILDUP | SHORT_BUILDUP |
                                                -- LONG_UNWINDING | SHORT_COVERING |
                                                -- FLAT | null

    -- ---- the swing read behind `structure` ---------------------------------
    swing_direction        text    NOT NULL,   -- up | down | none
    swing_high             numeric,            -- null until two swings confirm
    swing_low              numeric,
    structure_determined   boolean NOT NULL,   -- false = too few bars to say

    -- ---- the volatility read behind `regime` -------------------------------
    -- range_used is the day's range over the expected move SCALED BY
    -- sqrt(session_elapsed), so 1.5 means the same thing at 09:45 as at 15:15.
    range_used             numeric,
    session_elapsed        numeric,            -- fraction of the session, 0-1
    volatility_expanding   boolean NOT NULL,

    created_at             bigint  NOT NULL,

    PRIMARY KEY (security_id, instrument_type, snapshot_ts),
    FOREIGN KEY (security_id, instrument_type)
        REFERENCES algo.instrument_master (security_id, instrument_type)
);

COMMIT;
