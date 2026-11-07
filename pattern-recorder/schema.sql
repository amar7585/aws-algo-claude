-- pattern-recorder : schema addition
--
-- Target: Neon project "AI Trader APP" (nameless-mountain-15353651),
--         database Algo, schema algo.
--         CONFIRM THE PROJECT ID FIRST - a second project carries an `algo`
--         schema with the same table names and incompatible column types.
--
-- One NEW table. Purely additive: nothing existing is altered, and only
-- pattern-recorder writes it.
--
-- Conventions (see CLAUDE.md):
--   * every time value is epoch seconds, bigint. No timestamptz, ever.
--   * (security_id, instrument_type) is instrument identity; security_id is text.
--   * numeric for prices.
--
-- ROLLBACK:  DROP TABLE algo.pattern_events;

BEGIN;

-- ---------------------------------------------------------------------------
-- pattern_events : one row per v2 pattern event - a 5-min trigger at a level.
--
-- WHAT IT IS. What pattern-recorder RECOGNISED, as it recognised it: the family,
-- the level, the candle, the volume read at detection time. Definitions:
-- pattern-recorder/v2-pattern-definitions.md.
--
-- WHAT IT IS NOT.
--   * Not algo.session_observations - that table is hand-logged ground truth;
--     nothing automated ever writes it.
--   * No OI. At detection time the trigger bar's 5-min OI is never yet written
--     (measured 230/230 quarter-hours, 2026-11-05), so OI is JOINED AT READ TIME
--     from intraday_fno_data / fno_5m_snapshot, which are immutable and keyed by
--     time.
--   * No outcome. Forward moves are computed at read time from candle_5min;
--     entries and exits are decided per strategy, later.
--
-- GRAIN AND KEY. One row per (rules_version, trigger bar, family, subtype,
-- direction). rules_version is IN THE KEY so a change to the definitions writes
-- new rows beside the old ones rather than overwriting them.
--
-- WRITE MODE. Every run re-derives the whole session so far and upserts all of
-- it. The events are a deterministic function of closed bars, so re-writing is
-- idempotent and a missed run is filled by the next one. first_seen_* and
-- created_at are kept on conflict: they say when an event was FIRST recorded.
--
-- Prices are the FUTURE's (the active contract named in fut_security_id). An
-- option wall or max pain is an index strike converted with the trigger bar's
-- own basis; level_strike keeps the strike.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS algo.pattern_events (
    security_id            text    NOT NULL,   -- the underlying index ('13')
    instrument_type        text    NOT NULL,   -- 'INDEX'
    rules_version          text    NOT NULL,   -- patterns.RULES_VERSION
    trigger_ts             bigint  NOT NULL,   -- the 5-min bar that completed it
    family                 text    NOT NULL,   -- R1 | R2 | C1 | C2
    subtype                text    NOT NULL,   -- plain | wick | close_beyond | dip_reclaim | touch_hold | wick_reclaim | none
    direction              text    NOT NULL,   -- up | down

    fut_security_id        text    NOT NULL,   -- the future whose bars were read (FUTIDX)
    first_ts               bigint  NOT NULL,   -- the pattern's first bar
    trigger_close          numeric NOT NULL,
    extreme                numeric NOT NULL,
    level                  text    NOT NULL,   -- pdh | pdl | orh | orl | swing_high | swing_low | call_wall | put_wall | max_pain
    level_price            numeric NOT NULL,   -- future price
    level_strike           numeric,            -- walls / max pain only
    distance_pts           numeric NOT NULL,
    distance_pct           numeric NOT NULL,
    other_levels           text[]  NOT NULL,
    candles                text[]  NOT NULL,
    prior_tests            integer,
    chart_pattern          text,               -- double_bottom | triple_top | multi_test_bottom | ...
    structure_15m          text    NOT NULL,   -- up | down | mixed | none
    prior_break_direction  text,
    prior_break_ts         bigint,
    vol_z                  numeric,
    vol_z_max              numeric,
    rvol_tod               numeric,
    retest_vol_ratio       numeric,
    sweep_depth_pct        numeric,
    bars_beyond            integer,
    break_strength         numeric,
    index_twin_offset_pct  numeric,

    first_seen_snapshot_ts bigint  NOT NULL,   -- the run that first wrote it
    first_seen_at          bigint  NOT NULL,   -- that run's captured_at
    created_at             bigint  NOT NULL,
    updated_at             bigint  NOT NULL,

    PRIMARY KEY (security_id, instrument_type, rules_version, trigger_ts, family, subtype, direction),
    FOREIGN KEY (security_id, instrument_type)
        REFERENCES algo.instrument_master (security_id, instrument_type),
    CONSTRAINT pattern_events_family    CHECK (family IN ('R1', 'R2', 'C1', 'C2')),
    CONSTRAINT pattern_events_direction CHECK (direction IN ('up', 'down'))
);

COMMIT;
