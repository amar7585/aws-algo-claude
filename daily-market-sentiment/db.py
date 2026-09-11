"""
Neon access.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo. That is the only database this function touches - a second Neon
project carries an `algo` schema with the same table names and incompatible
column types, so never resolve the connection string from anywhere but
/algo/neon/connection.

connect() lives in the neon-access layer. pg8000's DB-API layer uses `format`
paramstyle (%s placeholders) and has no execute_values equivalent, so the
multi-row upserts below build their own VALUES tuples.
"""

import logging

from config import UPSERT_BATCH_SIZE

logger = logging.getLogger()


def resolve_instrument(conn, security_id, instrument_type):
    """
    Resolve on the FULL identity, never security_id alone.

    security_id is NOT unique. 19 security_ids in instrument_master carry more
    than one instrument_type, and `13` is both NIFTY (IDX_I/INDEX) and ABB
    (NSE_EQ/EQUITY). An earlier version of this function queried on
    security_id alone and took fetchone(); on 2026-09-11 it silently resolved
    to ABB and wrote 204 ABB candles plus an ABB sentiment row labelled NIFTY.

    fetchall() plus an exactly-one check means a future collision raises
    instead of picking whichever row Postgres happens to return first.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT security_id, trading_symbol, exchange_segment, instrument_type "
        "FROM algo.instrument_master "
        "WHERE security_id = %s AND instrument_type = %s",
        (str(security_id), str(instrument_type)),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"{security_id}/{instrument_type} matched {len(rows)} rows in "
            f"algo.instrument_master, expected exactly 1"
            + ("" if rows else " - run instrument-master-loader first")
        )
    row = rows[0]
    instrument = {
        "security_id": row[0],
        "trading_symbol": row[1],
        "exchange_segment": row[2],
        "instrument_type": row[3],
    }
    logger.info(
        "resolved %s/%s -> %s on %s",
        security_id, instrument_type, instrument["trading_symbol"],
        instrument["exchange_segment"],
    )
    return instrument


def latest_candle_ts(conn, instrument):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT MAX(candle_ts) FROM algo.candle_daily "
        "WHERE security_id = %s AND instrument_type = %s",
        (instrument["security_id"], instrument["instrument_type"]),
    )
    row = cursor.fetchone()
    return row[0] if row and row[0] is not None else None


def load_daily_candles(conn, instrument):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT candle_ts, open, high, low, close, volume FROM algo.candle_daily "
        "WHERE security_id = %s AND instrument_type = %s ORDER BY candle_ts",
        (instrument["security_id"], instrument["instrument_type"]),
    )
    return [
        {
            "ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
            "low": float(r[3]), "close": float(r[4]), "volume": int(r[5]),
        }
        for r in cursor.fetchall()
    ]


CANDLE_UPSERT = """
INSERT INTO algo.candle_daily
    (security_id, instrument_type, candle_ts, open, high, low, close, volume)
VALUES {values}
ON CONFLICT (security_id, instrument_type, candle_ts) DO UPDATE SET
    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
    close = EXCLUDED.close, volume = EXCLUDED.volume
"""


def upsert_daily_candles(conn, instrument, candles):
    if not candles:
        return 0
    cursor = conn.cursor()
    written = 0
    for start in range(0, len(candles), UPSERT_BATCH_SIZE):
        batch = candles[start : start + UPSERT_BATCH_SIZE]
        params = []
        for candle in batch:
            params.extend([
                instrument["security_id"], instrument["instrument_type"],
                candle["ts"], candle["open"], candle["high"], candle["low"],
                candle["close"], int(candle["volume"]),
            ])
        placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s)"] * len(batch))
        cursor.execute(CANDLE_UPSERT.format(values=placeholders), params)
        written += len(batch)
    conn.commit()
    return written


SENTIMENT_FIELDS = (
    "security_id", "instrument_type", "trade_date", "bias", "structure",
    "regime", "score", "confidence", "pd_high", "pd_low", "pd_close",
    "rsi", "sma20", "sma50", "sma100", "sma200", "prev_volume",
    "avg_volume_50", "vix", "price", "expected_move", "upper_volatility",
    "lower_volatility", "min15_high", "min15_low", "created_at",
)

SENTIMENT_UPSERT = """
INSERT INTO algo.daily_market_sentiment ({columns})
VALUES ({placeholders})
ON CONFLICT (security_id, instrument_type, trade_date) DO UPDATE SET
{updates}
""".format(
    columns=", ".join(SENTIMENT_FIELDS),
    placeholders=", ".join(["%s"] * len(SENTIMENT_FIELDS)),
    updates=",\n".join(
        f"    {f} = EXCLUDED.{f}"
        for f in SENTIMENT_FIELDS
        if f not in ("security_id", "instrument_type", "trade_date")
    ),
)


def upsert_sentiment(conn, row):
    cursor = conn.cursor()
    cursor.execute(SENTIMENT_UPSERT, tuple(row[f] for f in SENTIMENT_FIELDS))
    conn.commit()
