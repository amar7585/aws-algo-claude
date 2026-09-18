-- observations : schema addition
--
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651),
--         database Algo, schema algo.
--
-- One table. Not written by any Lambda function — this is manually logged
-- during the current observation-only phase (see CLAUDE.md, "not trading").
--
-- Conventions (see CLAUDE.md):
--   * every time value is epoch seconds, bigint. No timestamptz, ever.
--   * (security_id, instrument_type) is instrument identity; security_id is text.
--   * numeric for prices.

BEGIN;

-- ---------------------------------------------------------------------------
-- session_observations : one row per chart pattern spotted on a live session,
-- logged by hand while reviewing a chart against what algo.intraday_market_sentiment
-- and algo.option_chain_snapshot captured at the same moment.
--
-- PURPOSE. The current phase is "identify the trades not to take", not
-- automated entries. Each row pairs a chart-read pattern (hammer, sweep,
-- double top, ...) with what the OI/PCR/buildup data said at that instant, so
-- a growing set of these rows becomes labeled training data later: does the
-- captured sentiment/OI state agree with or contradict what price actually
-- did next.
--
-- GRAIN. One row per (security_id, instrument_type, event_ts, pattern_type).
-- event_ts is the 5-minute candle where the pattern was spotted on the chart,
-- NOT rounded to the sentiment snapshot grid — nearest_snapshot_ts links to
-- the closest algo.intraday_market_sentiment row for feature lookup, and is
-- NULL when the pattern falls before the first snapshot of the day (10:00 -
-- see intraday-market-sentiment/README.md; nothing is captured 09:15-09:55).
--
-- RESOLUTION IS DATA-DRIVEN, NOT A FIXED WINDOW. A pattern's outcome is
-- measured against whatever candle/signal actually closed the leg it opened -
-- the next reversal spotted on the same session, or the 15:25 close if the
-- session ends before one shows up (INTRADAY ONLY: no row's resolved_ts
-- crosses to a different day). resolved_ts/resolved_price are NULL until
-- that next point is known, at which point forward_return_pct is filled in.
-- This is deliberately not "N bars later": a fixed N would average a 10-minute
-- reversal leg and a 90-minute one together as if they were the same kind of
-- measurement.
--
-- outcome AND signal_agreement ARE SEPARATE. outcome is what price did
-- (reversal / continuation / no_signal) - a read of the chart. signal_agreement
-- is whether the stored buildup/PCR/IV-skew state at nearest_snapshot_ts
-- pointed the same way BEFORE the outcome was known - the actual thing being
-- tested. Collapsing them into one column would make it impossible to later
-- ask "how often does the data agree with the chart" as a standalone question.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo.session_observations (
    security_id           text    NOT NULL,
    instrument_type       text    NOT NULL,
    event_ts              bigint  NOT NULL,   -- candle where the pattern was spotted
    nearest_snapshot_ts   bigint,             -- algo.intraday_market_sentiment row for features; NULL before 10:00

    pattern_type           text    NOT NULL,   -- 'orb_sweep' | 'hammer' | 'shooting_star' | 'double_top' | 'double_bottom' | ...
    price_at_event         numeric NOT NULL,

    resolved_ts             bigint,             -- event_ts of whatever closed this leg; NULL until known
    resolved_price           numeric,
    forward_return_pct       numeric,            -- (resolved_price - price_at_event) / price_at_event * 100

    outcome                  text    NOT NULL,   -- 'reversal' | 'continuation' | 'no_signal' -- what price did
    signal_agreement         text,               -- 'confirmed' | 'contradicted' | 'no_data'  -- did the stored sentiment/OI state agree

    notes                    text,
    created_at               bigint  NOT NULL,

    PRIMARY KEY (security_id, instrument_type, event_ts, pattern_type),
    FOREIGN KEY (security_id, instrument_type)
        REFERENCES algo.instrument_master (security_id, instrument_type),
    CONSTRAINT session_observations_outcome
        CHECK (outcome IN ('reversal', 'continuation', 'no_signal')),
    CONSTRAINT session_observations_agreement
        CHECK (signal_agreement IS NULL OR signal_agreement IN ('confirmed', 'contradicted', 'no_data'))
);

COMMIT;
