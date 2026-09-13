-- algo.trading_holiday - the days the exchange is closed on a weekday.
--
-- HOLIDAYS ONLY, NOT A SESSION CALENDAR. A row means "the exchange is shut on
-- this date". No row means a normal session. Absence is therefore the safe
-- default: a calendar that has not been reseeded fails towards trading, which
-- costs a few no-op invocations, rather than towards silence, which costs a
-- whole session and alarms nothing.
--
-- Weekends are NOT stored. Nothing reads this table at a weekend: every cron
-- in the system is MON-FRI, so a Saturday is already excluded before the
-- question is asked. Storing ~104 rows a year to restate that would be noise.
--
-- trade_date IS A `date`, NOT EPOCH SECONDS - a deliberate departure from the
-- repo-wide rule that every stored time value is epoch seconds (bigint).
-- That rule exists so stored INSTANTS are unambiguous. A trading holiday is
-- not an instant; it is a calendar date. Epoch-encoding it would force an
-- arbitrary "midnight in which zone" convention on every reader, and getting
-- that wrong is silent - the exact failure the epoch rule was written to
-- prevent. `updated_at` below IS an instant, so it stays epoch seconds.
--
-- Writes to Neon project "AI Trader APP" (nameless-mountain-15353651),
-- database Algo, schema algo. The other Neon project's `algo` schema is a
-- DIFFERENT database with incompatible columns - confirm the project id.

CREATE TABLE IF NOT EXISTS algo.trading_holiday (
    trade_date   date PRIMARY KEY,
    description  text,
    source       text   NOT NULL,
    updated_at   bigint NOT NULL
);

COMMENT ON TABLE algo.trading_holiday IS
    'Weekday exchange holidays. A row means closed; no row means a normal '
    'session. Weekends are excluded by every cron and are not stored.';
COMMENT ON COLUMN algo.trading_holiday.trade_date IS
    'Calendar date, not epoch - see the header note in schema.sql.';
COMMENT ON COLUMN algo.trading_holiday.description IS
    'Holiday name where it is known with certainty. NULL is expected: the '
    'seed source publishes dates without names, and a wrong label on a '
    'movable festival is worse than none.';
COMMENT ON COLUMN algo.trading_holiday.source IS
    'Provenance of the row, e.g. "exchange_calendars XBOM 4.13.2".';
COMMENT ON COLUMN algo.trading_holiday.updated_at IS
    'Epoch seconds when the row was last written. An instant, so epoch.';
