"""
Neon access - reads only.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo. That is the only database this function touches - a second Neon
project carries an `algo` schema with the same table names and incompatible
column types, so never resolve the connection string from anywhere but
/algo/neon/connection.

THIS FUNCTION WRITES NOTHING. There is no INSERT, no UPDATE and no upsert
here, and that is the design rather than an omission: the manager's
output is the invocation it makes, and the regime it decided on travels in
that payload for the strategy to record. Nothing it computes is persisted by
it, so nothing it computes can go stale in a table.

connect() lives in the neon-access layer. pg8000's DB-API layer uses `format`
paramstyle (%s placeholders).
"""

import logging

logger = logging.getLogger()

_INSTRUMENT_COLUMNS = (
    "SELECT security_id, trading_symbol, exchange_segment, instrument_type "
    "FROM algo.instrument_master "
)

CANDLE_COLUMNS = ("candle_ts", "open", "high", "low", "close", "volume")

# Which table holds which interval. Mirrors intraday-data-loader's
# INTERVAL_TABLES, and deliberately a copy - a shared version would have to
# live in the neon-access layer, where every change means republishing it and
# repointing every function's pinned ARN.
INTERVAL_TABLES = {
    5: "algo.candle_5min",
    15: "algo.candle_15min",
    60: "algo.candle_1hr",
}

DAILY_TABLE = "algo.candle_daily"
# THE DAILY SENTIMENT ROW IS NOT READ HERE. It arrives in the manager's
# payload, already looked up by intraday-market-sentiment. Reading it again
# would be a second answer to the same question, and the two could differ -
# the payload's row is the one the routing decision was actually made
# against, so it is the one this playbook must gate on.

# Everything the sweep playbook's gate and its levels need off the daily row.
# pd_high/pd_low are the previous day's extremes - two of the liquidity pools -
# and upper/lower_volatility bracket the VIX-implied move for the day.


def closed_candles(conn, interval_minutes, instrument, as_of, limit):
    """
    The newest `limit` CLOSED bars at or before `as_of`, oldest first.

    THE CLOSURE RULE IS THE DOCUMENTED READER'S RULE, not a guess:
    intraday-data-loader stores the in-progress bucket on purpose and corrects
    it by primary key on a later pass, so a consumer tells a finished bar from
    a forming one with `candle_ts + interval_seconds <= now`. `as_of` stands in
    for now, and it is derived from the snapshot rather than the clock so that
    two runs over the same snapshot read exactly the same bars.

    Ordered oldest-first on return because every indicator in indicators.py
    walks forward, and a reversed series produces a plausible number rather
    than an error.
    """
    table = INTERVAL_TABLES.get(int(interval_minutes))
    if table is None:
        raise RuntimeError(
            f"no table for a {interval_minutes}-minute interval - "
            f"intraday-data-loader fills {sorted(INTERVAL_TABLES)}"
        )
    interval_seconds = int(interval_minutes) * 60
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(CANDLE_COLUMNS)} FROM {table} "
        f"WHERE security_id = %s AND instrument_type = %s "
        f"AND candle_ts + %s <= %s "
        f"ORDER BY candle_ts DESC LIMIT %s",
        (
            str(instrument["security_id"]),
            str(instrument["instrument_type"]),
            interval_seconds,
            int(as_of),
            int(limit),
        ),
    )
    rows = cursor.fetchall()
    candles = [
        {
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": int(row[5] or 0),
        }
        for row in reversed(rows)
    ]
    logger.info(
        "%s: %d closed bars up to %d", table, len(candles), int(as_of)
    )
    return candles


def daily_candles(conn, instrument, before_ts, limit):
    """
    The newest `limit` daily bars strictly before `before_ts`, oldest first.

    Strictly before, because `before_ts` is IST midnight of the session in
    progress and today's daily candle is not a finished bar. Dhan's daily
    endpoint lags a session anyway, so it will not normally be there - but
    "will not normally be" is not a filter.
    """
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT {', '.join(CANDLE_COLUMNS)} FROM {DAILY_TABLE} "
        f"WHERE security_id = %s AND instrument_type = %s AND candle_ts < %s "
        f"ORDER BY candle_ts DESC LIMIT %s",
        (
            str(instrument["security_id"]),
            str(instrument["instrument_type"]),
            int(before_ts),
            int(limit),
        ),
    )
    rows = cursor.fetchall()
    return [
        {
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": int(row[5] or 0),
        }
        for row in reversed(rows)
    ]
