"""
Neon access.

Target: project "AI Trader APP" (nameless-mountain-15353651), database Algo,
schema algo. That is the only database this function touches - a second Neon
project carries an `algo` schema with the same table names and incompatible
column types, so never resolve the connection string from anywhere but
/algo/neon/connection.

connect() lives in the neon-access layer. pg8000's DB-API layer uses `format`
paramstyle (%s placeholders) and has no execute_values equivalent, so the
multi-row upsert below builds its own VALUES tuples.

candle_5min, candle_15min and candle_1hr are byte-identical in shape to
candle_daily - same columns, same primary key, same foreign key back to
instrument_master - so one upsert serves all three with the table name swapped.
That name is never taken from input: it comes from the INTERVAL_TABLES mapping
in config.py and is checked against it before reaching a query.
"""

import logging

from config import INTERVAL_TABLES, UPSERT_BATCH_SIZE

logger = logging.getLogger()

_KNOWN_TABLES = frozenset(INTERVAL_TABLES.values())


def _checked_table(table):
    if table not in _KNOWN_TABLES:
        raise RuntimeError(
            f"{table!r} is not one of the candle tables {sorted(_KNOWN_TABLES)}"
        )
    return table


def _instrument(row):
    return {
        "security_id": row[0],
        "trading_symbol": row[1],
        "exchange_segment": row[2],
        "instrument_type": row[3],
    }


_INSTRUMENT_COLUMNS = (
    "SELECT security_id, trading_symbol, exchange_segment, instrument_type "
    "FROM algo.instrument_master "
)


def resolve_instrument(conn, security_id, instrument_type):
    """
    Resolve on the FULL identity, never security_id alone.

    security_id is NOT unique. 19 security_ids in instrument_master carry more
    than one instrument_type, and `13` is both NIFTY (IDX_I/INDEX) and ABB
    (NSE_EQ/EQUITY). A lookup on security_id alone silently resolved to ABB on
    2026-09-11 in daily-market-sentiment and wrote 204 ABB candles labelled
    NIFTY.

    fetchall() plus an exactly-one check means a future collision raises
    instead of picking whichever row Postgres happens to return first.
    """
    cursor = conn.cursor()
    cursor.execute(
        _INSTRUMENT_COLUMNS + "WHERE security_id = %s AND instrument_type = %s",
        (str(security_id), str(instrument_type)),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"{security_id}/{instrument_type} matched {len(rows)} rows in "
            f"algo.instrument_master, expected exactly 1"
            + ("" if rows else " - run instrument-master-loader first")
        )
    instrument = _instrument(rows[0])
    logger.info(
        "resolved %s/%s -> %s on %s",
        security_id, instrument_type, instrument["trading_symbol"],
        instrument["exchange_segment"],
    )
    return instrument


def resolve_by_symbol(conn, trading_symbol, instrument_type):
    """
    Resolve a contract by its trading symbol - used for the current-month
    future, whose security_id changes every month and is therefore never
    hardcoded.

    The symbol is built from the live expiry list (see handler.current_future).
    If it is missing here the instrument master is stale rather than the rule
    being wrong, and the message says so: these tables carry a foreign key
    back to instrument_master, so a missing contract would fail on insert
    anyway, just less legibly.
    """
    cursor = conn.cursor()
    cursor.execute(
        _INSTRUMENT_COLUMNS + "WHERE trading_symbol = %s AND instrument_type = %s",
        (str(trading_symbol), str(instrument_type)),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"{trading_symbol}/{instrument_type} matched {len(rows)} rows in "
            f"algo.instrument_master, expected exactly 1"
            + (
                ""
                if rows
                else " - the instrument master predates this contract; run "
                "instrument-master-loader"
            )
        )
    instrument = _instrument(rows[0])
    logger.info(
        "resolved %s -> security_id %s on %s",
        trading_symbol, instrument["security_id"], instrument["exchange_segment"],
    )
    return instrument


def latest_candle_ts(conn, instrument, table):
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT MAX(candle_ts) FROM {_checked_table(table)} "
        "WHERE security_id = %s AND instrument_type = %s",
        (instrument["security_id"], instrument["instrument_type"]),
    )
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else None


CANDLE_UPSERT = """
INSERT INTO {table}
    (security_id, instrument_type, candle_ts, open, high, low, close, volume)
VALUES {values}
ON CONFLICT (security_id, instrument_type, candle_ts) DO UPDATE SET
    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
    close = EXCLUDED.close, volume = EXCLUDED.volume
"""


def upsert_candles(conn, instrument, table, candles):
    """
    Write candles, correcting any bar already stored.

    The DO UPDATE is not incidental - it is the mechanism that finalises
    partial bars. A bar written while its bucket is still forming is
    overwritten by the same key on a later pass, once the bucket has closed.
    """
    if not candles:
        return 0
    table = _checked_table(table)
    cursor = conn.cursor()
    written = 0
    try:
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
            cursor.execute(
                CANDLE_UPSERT.format(table=table, values=placeholders), params
            )
            written += len(batch)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return written
